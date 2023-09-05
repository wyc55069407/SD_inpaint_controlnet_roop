from PIL import Image
from diffusers import UniPCMultistepScheduler, EulerAncestralDiscreteScheduler, StableDiffusionControlNetInpaintPipeline, ControlNetModel
import torch
import numpy as np
import argparse
from typing import Union, List, Optional, Tuple
from diffusers.utils import load_image
from diffusers.pipeline_utils import DiffusionPipeline
from transformers import CLIPTokenizer
from openvino.runtime import Core, Model, Type
from openvino.runtime.passes import Manager, GraphRewrite, MatcherPass, WrapType, Matcher
from openvino.runtime import opset10 as ops
from safetensors.torch import load_file
import time
import cv2
from diffusers.image_processor import VaeImageProcessor
import diffusers.image_processor

def parse_args() -> argparse.Namespace:
    """Parse and return command line arguments."""
    parser = argparse.ArgumentParser(add_help=False)
    args = parser.add_argument_group('Options')
    # fmt: off
    args.add_argument('-h', '--help', action = 'help',
                      help='Show this help message and exit.')
    args.add_argument('-lp', '--lora_path', type = str, default = "", required = False,
                      help='Specify path of lora weights *.safetensors')
    args.add_argument('-a','--alpha',type = float, default = 0.75, required = False,
                      help='Specify the merging ratio of lora weights, default is 0.75.')
    args.add_argument('-lp2', '--lora_path2', type = str, default = "", required = False,
                      help='Specify path of lora weights *.safetensors')
    args.add_argument('-a2','--alpha2',type = float, default = 0.75, required = False,
                      help='Specify the merging ratio of lora weights, default is 0.75.')                  
    return parser.parse_args()

def scale_fit_to_window(dst_width:int, dst_height:int, image_width:int, image_height:int):
    im_scale = min(dst_height / image_height, dst_width / image_width)
    return int(im_scale * image_width), int(im_scale * image_height)

def preprocess(image: Image.Image):
    src_width, src_height = image.size
    dst_width, dst_height = scale_fit_to_window(512, 512, src_width, src_height)
    image = np.array(image.resize((dst_width, dst_height), resample=Image.Resampling.LANCZOS))[None, :]
    pad_width = 512 - dst_width
    pad_height = 512 - dst_height
    pad = ((0, 0), (0, pad_height), (0, pad_width), (0, 0))
    image = np.pad(image, pad, mode="constant")
    #image = np.squeeze(image)
    #image = cv2.copyMakeBorder(image, int(pad_height//2), 512-int(pad_height//2)-dst_height, int(pad_width//2), 512-int(pad_width//2)-dst_width, cv2.BORDER_CONSTANT, (0,0,0) );
    #cv2.imwrite("preprocess.png",image)
    #image = np.expand_dims(image, axis=0)
    image = image.astype(np.float32) / 255.0
    image = image.transpose(0, 3, 1, 2)
    return image, pad


def randn_tensor(
    shape: Union[Tuple, List],
    dtype: Optional[np.dtype] = np.float32,
):
    latents = np.random.randn(*shape).astype(dtype)

    return latents

class InsertLoRA(MatcherPass):
    def __init__(self,lora_dict_list):
        MatcherPass.__init__(self)
        self.model_changed = False

        param = WrapType("opset10.Convert")

        def callback(matcher: Matcher) -> bool:
            root = matcher.get_match_root()
            root_output = matcher.get_match_value()
            for y in lora_dict_list:
                if root.get_friendly_name().replace('.','_').replace('_weight','') == y["name"]:
                    consumers = root_output.get_target_inputs()
                    lora_weights = ops.constant(y["value"],Type.f32,name=y["name"])
                    add_lora = ops.add(root,lora_weights,auto_broadcast='numpy')
                    for consumer in consumers:
                        consumer.replace_source_output(add_lora.output(0))

                    # For testing purpose
                    self.model_changed = True
                    # Use new operation for additional matching
                    self.register_new_node(add_lora)

            # Root node wasn't replaced or changed
            return False

        self.register_matcher(Matcher(param,"InsertLoRA"), callback)

g_device = None
g_vae = None

class OVContrlNetStableDiffusionPipeline(DiffusionPipeline):
    """
    OpenVINO inference pipeline for Stable Diffusion with ControlNet guidence
    """
    def __init__(
        self,
        tokenizer: CLIPTokenizer,
        scheduler,
        core: Core,
        controlnet: Model,
        text_encoder: Model,
        unet: Model,
        vae_decoder: Model,
        vae_encoder: Model,
        state_dict,
        alpha_list,
        device:str = "AUTO"
    ):
        super().__init__()
        self.tokenizer = tokenizer
        self.vae_scale_factor = 8 #2 ** (len(self.vae.config.block_out_channels) - 1)
        self.scheduler = scheduler
        self.load_models(core, device, controlnet, text_encoder, unet, vae_decoder, vae_encoder, state_dict, alpha_list)
        self.set_progress_bar_config(disable=True)
        g_device = device
        
        self.image_processor = VaeImageProcessor(vae_scale_factor=self.vae_scale_factor)
        self.control_image_processor = VaeImageProcessor(
            vae_scale_factor=self.vae_scale_factor, do_convert_rgb=True, do_normalize=False
        )
    

    def load_models(self, core: Core, device: str, controlnet:Model, text_encoder: Model, unet: Model, vae_decoder: Model, vae_encoder: Model, state_dict, alpha_list):
        if state_dict != None:
            ov_unet = core.read_model(unet)
            ov_text_encoder = core.read_model(text_encoder)
            ##===Add lora weights===
            visited = []
            lora_dict = {}
            lora_dict_list = []
            LORA_PREFIX_UNET = "lora_unet"
            LORA_PREFIX_TEXT_ENCODER = "lora_te"
            flag = 0
            manager = Manager()
            for iter in range(len(state_dict)):
                visited = []
                for key in state_dict[iter]:
                    if ".alpha" in key or key in visited:
                        continue
                    if "text" in key:
                        layer_infos = key.split(LORA_PREFIX_TEXT_ENCODER + "_")[-1].split(".")[0]
                        lora_dict = dict(name=layer_infos)
                        lora_dict.update(type="text_encoder")
                    else:
                        layer_infos = key.split(LORA_PREFIX_UNET + "_")[1].split('.')[0]
                        lora_dict = dict(name=layer_infos)
                        lora_dict.update(type="unet")
                    pair_keys = []
                    if "lora_down" in key:
                        pair_keys.append(key.replace("lora_down", "lora_up"))
                        pair_keys.append(key)
                    else:
                        pair_keys.append(key)
                        pair_keys.append(key.replace("lora_up", "lora_down"))

                        # update weight
                    if len(state_dict[iter][pair_keys[0]].shape) == 4:
                        weight_up = state_dict[iter][pair_keys[0]].squeeze(3).squeeze(2).to(torch.float32)
                        weight_down = state_dict[iter][pair_keys[1]].squeeze(3).squeeze(2).to(torch.float32)
                        lora_weights = alpha_list[iter] * torch.mm(weight_up, weight_down).unsqueeze(2).unsqueeze(3)
                        lora_dict.update(value=lora_weights)
                    else:
                        weight_up = state_dict[iter][pair_keys[0]].to(torch.float32)
                        weight_down = state_dict[iter][pair_keys[1]].to(torch.float32)
                        lora_weights = alpha_list[iter] * torch.mm(weight_up, weight_down)
                        lora_dict.update(value=lora_weights)
                    #check if this layer has been appended in lora_dict_list
                    for ll in lora_dict_list:
                        if ll["name"] == lora_dict["name"]:
                            ll["value"] += lora_dict["value"] # all lora weights added together
                            flag = 1
                    if flag == 0:
                        lora_dict_list.append(lora_dict)
                    # update visited list
                    for item in pair_keys:
                        visited.append(item)
                    flag = 0
            manager.register_pass(InsertLoRA(lora_dict_list))
            if (True in [('type','text_encoder') in l.items() for l in lora_dict_list]):
                manager.run_passes(ov_text_encoder)
            self.text_encoder = core.compile_model(ov_text_encoder, device)
            manager.run_passes(ov_unet)
            self.unet = core.compile_model(ov_unet, device)
        else:
            self.text_encoder = core.compile_model(text_encoder, device)
            self.unet = core.compile_model(unet, device)

        self.text_encoder_out = self.text_encoder.output(0)
        self.controlnet = core.compile_model(controlnet, device)
        self.unet_out = self.unet.output(0)
        self.vae_decoder = core.compile_model(vae_decoder)
        self.vae_decoder_out = self.vae_decoder.output(0)
        self.vae_encoder = core.compile_model(vae_encoder)
        self.vae_encoder_out = self.vae_encoder.output(0)
    def prepare_image(self):
        height = 512
        width = 512
        pad_height = 0
        pad_width = 0
        pad = ((0, 0), (0, pad_height), (0, pad_width), (0, 0))
        return height, width, pad
        
    def prepare_control_image(
        self,
        image,
        width,
        height,
        batch_size,
        num_images_per_prompt,
        device,
        dtype,
        do_classifier_free_guidance=False,
        guess_mode=False,
    ):
        image = self.control_image_processor.preprocess(image, height=height, width=width).to(dtype=torch.float32)
        image_batch_size = image.shape[0]

        if image_batch_size == 1:
            repeat_by = batch_size
        else:
            # image batch size is the same as prompt batch size
            repeat_by = num_images_per_prompt

        image = image.repeat_interleave(repeat_by, dim=0)

        image = image.to(device=device, dtype=dtype)

        if do_classifier_free_guidance and not guess_mode:
            image = torch.cat([image] * 2)

        return image
    def prepare_mask_and_masked_image(self, image, mask, height, width, return_image=False):
        if image is None:
            raise ValueError("`image` input cannot be undefined.")
        
        if mask is None:
            raise ValueError("`mask_image` input cannot be undefined.")
        
        # preprocess image
        if isinstance(image, (Image.Image, np.ndarray)):
            image = [image]
        if isinstance(image, list) and isinstance(image[0], Image.Image):
            # resize all images w.r.t passed height an width
            image = [i.resize((width, height), resample=Image.LANCZOS) for i in image]
            image = [np.array(i.convert("RGB"))[None, :] for i in image]
            image = np.concatenate(image, axis=0)
        elif isinstance(image, list) and isinstance(image[0], np.ndarray):
            image = np.concatenate([i[None, :] for i in image], axis=0)
        
        image = image.transpose(0, 3, 1, 2)
        #image = image.astype(np.float32) / 127.5 - 1.0
        image = torch.from_numpy(image).to(dtype=torch.float32) / 127.5 - 1.0
        
        # preprocess mask
        if isinstance(mask, (Image.Image, np.ndarray)):
            mask = [mask]
        
        if isinstance(mask, list) and isinstance(mask[0], Image.Image):
            mask = [i.resize((width, height), resample=Image.LANCZOS) for i in mask]
            mask = np.concatenate([np.array(m.convert("L"))[None, None, :] for m in mask], axis=0)
            mask = mask.astype(np.float32) / 255.0
        elif isinstance(mask, list) and isinstance(mask[0], np.ndarray):
            mask = np.concatenate([m[None, None, :] for m in mask], axis=0)
        
        mask[mask < 0.5] = 0
        mask[mask >= 0.5] = 1
        mask = torch.from_numpy(mask)
        
        masked_image = image * (mask < 0.5)

        # n.b. ensure backwards compatibility as old function does not return image
        if return_image:
            return mask, masked_image, image
        
        return mask, masked_image
        
    def prepare_mask_latents(
        self, mask, masked_image, batch_size, height, width, dtype, do_classifier_free_guidance
    ):
        # resize the mask to latents shape as we concatenate the mask to the latents
        # we do that before converting to dtype to avoid breaking in case we're using cpu_offload
        # and half precision
        mask = torch.nn.functional.interpolate(
            mask, size=(height // self.vae_scale_factor, width // self.vae_scale_factor)
        )
        mask = mask.to(device=g_device, dtype=dtype)

        masked_image = masked_image.to(device=g_device, dtype=dtype)

        # duplicate mask and masked_image_latents for each generation per prompt, using mps friendly method
        if mask.shape[0] < batch_size:
            if not batch_size % mask.shape[0] == 0:
                raise ValueError(
                    "The passed mask and the required batch size don't match. Masks are supposed to be duplicated to"
                    f" a total batch size of {batch_size}, but {mask.shape[0]} masks were passed. Make sure the number"
                    " of masks that you pass is divisible by the total requested batch size."
                )
            mask = mask.repeat(batch_size // mask.shape[0], 1, 1, 1)

        mask = torch.cat([mask] * 2) if do_classifier_free_guidance else mask
        
        mask = mask.to('cpu').detach().numpy()
        return mask
    
    def _encode_vae_image(self, image):
        image = image.to('cpu').detach().numpy()
        image_latents = self.vae_encoder(image)[self.vae_encoder_out]
        image_latents = 0.18215 * image_latents
    
        return image_latents

    def __call__(
        self,
        prompt: Union[str, List[str]],
        init_image: Image.Image,
        mask_image: Image.Image,
        control_image: torch.Tensor,#Image.Image,
        num_inference_steps: int = 10,
        negative_prompt: Union[str, List[str]] = None,
        guidance_scale: float = 7.5,
        controlnet_conditioning_scale: Union[float, List[float]] = 1.0,
        control_guidance_start: Union[float, List[float]] = [0.0], #single controlnet
        control_guidance_end: Union[float, List[float]] = [1.0], #single controlnet
        eta: float = 0.0,
        latents: Optional[np.array] = None,
        output_type: Optional[str] = "pil",
    ):

        # 1. Define call parameters
        batch_size = 1 if isinstance(prompt, str) else len(prompt)

        do_classifier_free_guidance = guidance_scale > 1.0
        # 2. Encode input prompt
        text_embeddings = self._encode_prompt(prompt, negative_prompt=negative_prompt)

        # 3. Preprocess image
        height, width, pad = self.prepare_image()
        orig_width = width
        orig_height = height
        dtype = torch.float32
        num_images_per_prompt = 1
        control_image = self.prepare_control_image(
            image=control_image,
            width=width,
            height=height,
            batch_size=batch_size * num_images_per_prompt,
            num_images_per_prompt=num_images_per_prompt,
            device=g_device,
            dtype=dtype,
            do_classifier_free_guidance=do_classifier_free_guidance,
            guess_mode=False,
        )

        # 4. Preprocess mask and image - resizes image and mask w.r.t height and width
        mask, masked_image, init_image = self.prepare_mask_and_masked_image(
            init_image, mask_image, height, width, return_image=True
        )

        # 4. set timesteps
        self.scheduler.set_timesteps(num_inference_steps)
        timesteps = self.scheduler.timesteps
        # at which timestep to set the initial noise (n.b. 50% if strength is 0.5)
        latent_timestep = timesteps[:1].repeat(batch_size)
        # create a boolean to check if the strength is set to 1. if so then initialise the latents with pure noise
        strength = 1.0
        is_strength_max = strength == 1.0
        
        # 6. Prepare latent variables
        num_channels_latents = 4
        return_image_latents = num_channels_latents == 4
        latents_outputs = self.prepare_latents(
            batch_size,
            num_channels_latents,
            height,
            width,
            text_embeddings.dtype,
            latents,
            image=init_image,
            timestep=latent_timestep,
            is_strength_max=is_strength_max,
            return_noise=True,
            return_image_latents=return_image_latents,
        )
        
        if return_image_latents:
            latents, noise, image_latents = latents_outputs
        else:
            latents, noise = latents_outputs
        
        text_embeddings_torch = torch.from_numpy(text_embeddings)
        # 6. Prepare mask latent variables
        mask = self.prepare_mask_latents(
            mask,
            masked_image,
            batch_size,
            height,
            width,
            text_embeddings_torch.dtype,
            do_classifier_free_guidance,
        )

         # 6.1 Create tensor stating which controlnets to keep
        controlnet_keep = []
        for i in range(len(timesteps)):
            keeps = [
                1.0 - float(i / len(timesteps) < s or (i + 1) / len(timesteps) > e)
                for s, e in zip(control_guidance_start, control_guidance_end)
            ]
            controlnet_keep.append(keeps[0]) #keeps[0] if isinstance(controlnet, ControlNetModel) else keeps)

        # 7. Denoising loop
        num_warmup_steps = len(timesteps) - num_inference_steps * self.scheduler.order
        with self.progress_bar(total=num_inference_steps) as progress_bar:
            for i, t in enumerate(timesteps):
                
                #if isinstance(latents, (torch.Tensor)):
                #    latents = latents.to('cpu').detach().numpy()
                print("Denoising loop\n")
                # Expand the latents if we are doing classifier free guidance.controlnet_pip
                # The latents are expanded 3 times because for pix2pix the guidance\
                # is applied for both the text and the input image.
                latent_model_input = np.concatenate(
                    [latents] * 2) if do_classifier_free_guidance else latents
                latent_model_input = self.scheduler.scale_model_input(latent_model_input, t)
                #text_embeddings = np.split(text_embeddings, 2)[1] if do_classifier_free_guidance else text_embeddings

                if isinstance(controlnet_keep[i], list):
                    cond_scale = [c * s for c, s in zip(controlnet_conditioning_scale, controlnet_keep[i])]
                else:
                    cond_scale = controlnet_conditioning_scale * controlnet_keep[i]
                
                result = self.controlnet([latent_model_input, t, text_embeddings, control_image.detach().numpy(), cond_scale])
                down_and_mid_blok_samples = [sample * cond_scale for _, sample in result.items()]

                # predict the noise residual
                noise_pred = self.unet([latent_model_input, t, text_embeddings, *down_and_mid_blok_samples])[self.unet_out]

                # perform guidance
                if do_classifier_free_guidance:
                    noise_pred_uncond, noise_pred_text = np.split(noise_pred,2) #noise_pred[0], noise_pred[1]
                    noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_text - noise_pred_uncond)

                # compute the previous noisy sample x_t -> x_t-1
                latents = self.scheduler.step(torch.from_numpy(noise_pred), t, torch.from_numpy(latents)).prev_sample.numpy()

                # num_channels_unet == 4
                init_latents_proper = image_latents[:1]
                init_mask = mask[:1]
                
                if i < len(timesteps) - 1:
                    noise_timestep = timesteps[i + 1]
                    init_latents_proper = self.scheduler.add_noise(
                        torch.from_numpy(init_latents_proper), torch.from_numpy(noise), torch.tensor([noise_timestep])
                    ).to('cpu').detach().numpy()
                latents = (1 - init_mask) * init_latents_proper + init_mask * latents

                # update progress
                if i == len(timesteps) - 1 or ((i + 1) > num_warmup_steps and (i + 1) % self.scheduler.order == 0):
                    progress_bar.update()
        
        if isinstance(latents, (torch.Tensor)):
            latents = latents.to('cpu').detach().numpy()
        # 8. Post-processing
        image = self.decode_latents(latents, pad)

        # 9. Convert to PIL
        if output_type == "pil":
            image = self.numpy_to_pil(image)
            image = [img.resize((orig_width, orig_height), Image.Resampling.LANCZOS) for img in image]
        else:
            image = [cv2.resize(img, (orig_width, orig_width))
                     for img in image]

        return image

    def _encode_prompt(self, prompt:Union[str, List[str]], num_images_per_prompt:int = 1, do_classifier_free_guidance:bool = True, negative_prompt:Union[str, List[str]] = None):
        batch_size = len(prompt) if isinstance(prompt, list) else 1

        # tokenize input prompts
        text_inputs = self.tokenizer(
            prompt,
            padding="max_length",
            max_length=self.tokenizer.model_max_length,
            truncation=True,
            return_tensors="np",
        )
        text_input_ids = text_inputs.input_ids

        text_embeddings = self.text_encoder(
            text_input_ids)[self.text_encoder_out]

        # duplicate text embeddings for each generation per prompt
        if num_images_per_prompt != 1:
            bs_embed, seq_len, _ = text_embeddings.shape
            text_embeddings = np.tile(
                text_embeddings, (1, num_images_per_prompt, 1))
            text_embeddings = np.reshape(
                text_embeddings, (bs_embed * num_images_per_prompt, seq_len, -1))

        # get unconditional embeddings for classifier free guidance
        if do_classifier_free_guidance:
            uncond_tokens: List[str]
            max_length = text_input_ids.shape[-1]
            if negative_prompt is None:
                uncond_tokens = [""] * batch_size
            elif isinstance(negative_prompt, str):
                uncond_tokens = [negative_prompt]
            else:
                uncond_tokens = negative_prompt
            uncond_input = self.tokenizer(
                uncond_tokens,
                padding="max_length",
                max_length=max_length,
                truncation=True,
                return_tensors="np",
            )

            uncond_embeddings = self.text_encoder(uncond_input.input_ids)[self.text_encoder_out]

            # duplicate unconditional embeddings for each generation per prompt, using mps friendly method
            seq_len = uncond_embeddings.shape[1]
            uncond_embeddings = np.tile(uncond_embeddings, (1, num_images_per_prompt, 1))
            uncond_embeddings = np.reshape(uncond_embeddings, (batch_size * num_images_per_prompt, seq_len, -1))

            # For classifier free guidance, we need to do two forward passes.
            # Here we concatenate the unconditional and text embeddings into a single batch
            # to avoid doing two forward passes
            text_embeddings = np.concatenate([uncond_embeddings, text_embeddings])

        return text_embeddings

    def prepare_latents(self, batch_size:int, num_channels_latents:int, height:int, width:int, dtype:np.dtype = np.float32, latents:np.ndarray = None,
        image=None,
        timestep=None,
        is_strength_max=True,
        return_noise=False,
        return_image_latents=False,):

        shape = (batch_size, num_channels_latents, height // self.vae_scale_factor, width // self.vae_scale_factor)
        if return_image_latents or (latents is None and not is_strength_max):
            image_latents = self._encode_vae_image(image=image)
        
        if latents is None:
            noise = randn_tensor(shape, dtype=dtype)
            # if strength is 1. then initialise the latents to noise, else initial to image + noise
            latents = noise if is_strength_max else self.scheduler.add_noise(image_latents, noise, timestep)
            # if pure noise then scale the initial latents by the  Scheduler's init sigma
            latents = latents * self.scheduler.init_noise_sigma if is_strength_max else latents
        else:
            noise = latents #.to(device)
            latents = noise * self.scheduler.init_noise_sigma

        outputs = (latents,)

        if return_noise:
            outputs += (noise,)

        if return_image_latents:
            outputs += (image_latents,)

        return outputs

    def decode_latents(self, latents:np.array, pad:Tuple[int]):

        latents = 1 / 0.18215 * latents # 1 / self.vae.config.scaling_factor * latents
        image = self.vae_decoder(latents)[self.vae_decoder_out]
        (_, end_h), (_, end_w) = pad[1:3]
        h, w = image.shape[2:]
        unpad_h = h - end_h
        unpad_w = w - end_w
        image = image[:, :, :unpad_h, :unpad_w]
        image = np.clip(image / 2 + 0.5, 0, 1)
        image = np.transpose(image, (0, 2, 3, 1))
        return image

def make_inpaint_condition(image, image_mask):
    image = np.array(image.convert("RGB")).astype(np.float32) / 255.0
    print(type(image))
    image_mask = np.array(image_mask.convert("L")).astype(np.float32) / 255.0

    assert image.shape[0:1] == image_mask.shape[0:1], "image and image_mask must have the same image size"
    image[image_mask > 0.5] = -1.0  # set as masked pixel
    image = np.expand_dims(image, 0).transpose(0, 3, 1, 2)

    image = torch.from_numpy(image)

    #image = DiffusionPipeline.numpy_to_pil(image)
    return image

def sd_inpaint(orignal_img, mask_img, prompt, negative_prompt):
    args = parse_args()
    controlnet = ControlNetModel.from_pretrained("./control_v11p_sd15_inpaint", torch_dtype=torch.float32).cpu()
    pipe = StableDiffusionControlNetInpaintPipeline.from_pretrained("../../lora_SDXL/stable-diffusion-v1-5", controlnet=controlnet)
    
    
    tokenizer = CLIPTokenizer.from_pretrained('../../lora_SDXL/clip-vit-large-patch14')
    scheduler = UniPCMultistepScheduler.from_config(pipe.scheduler.config)
    #scheduler = EulerAncestralDiscreteScheduler.from_config(pipe.scheduler.config)
    
    CONTROLNET_OV_PATH = "controlnet-inpaint.xml"
    TEXT_ENCODER_OV_PATH = "text_encoder.xml"
    UNET_OV_PATH = "unet_controlnet.xml"
    VAE_DECODER_OV_PATH = "vae_decoder.xml"
    VAE_ENCODER_OV_PATH = "vae_encoder.xml"
    
    core = Core()
    #core.set_property({'CACHE_DIR': './cache'})
    #====Add lora======
    LORA_PATH = []
    LORA_ALPHA = []
    if args.lora_path != "":
        LORA_PATH.append(args.lora_path)
        LORA_ALPHA.append(args.alpha)
        if args.lora_path2 != "":
            LORA_PATH.append(args.lora_path2)
            LORA_ALPHA.append(args.alpha)
    
    state_dict = []
    # load LoRA weight from .safetensors
    if len(LORA_PATH) == 0:
        ov_pipe = OVContrlNetStableDiffusionPipeline(tokenizer, scheduler, core, CONTROLNET_OV_PATH, TEXT_ENCODER_OV_PATH, UNET_OV_PATH, VAE_DECODER_OV_PATH, VAE_ENCODER_OV_PATH, None, None, device="GPU") #change to CPU or GPU
    else:
        [state_dict.append(load_file(p)) for p in LORA_PATH] #state_dict is list of lora list
        ov_pipe = OVContrlNetStableDiffusionPipeline(tokenizer, scheduler, core, CONTROLNET_OV_PATH, TEXT_ENCODER_OV_PATH, UNET_OV_PATH, VAE_DECODER_OV_PATH, VAE_ENCODER_OV_PATH, state_dict, LORA_ALPHA, device="GPU") #change to CPU or GPU
    
    init_image = load_image(
        orignal_img
    )
    init_image = init_image.resize((512, 512))
    mask_image = load_image(
        mask_img
    )
    mask_image = mask_image.resize((512, 512))
    
    control_image = make_inpaint_condition(init_image, mask_image)
    #prompt = ["a handsome prince with a pink MLB hat","a handsome man with ray-ban sunglasses"]
    #prompt = ["a cute child with blue NFL hat","a handsome man with ray-ban sunglasses"]
    
    num_steps = 20
    
    #negative_prompt = ["monochrome, lowres, bad anatomy, worst quality, low quality","monochrome, lowres, bad anatomy, worst quality, low quality"]
    
    np.random.seed(42)
    start = time.time()
    results = ov_pipe(prompt, init_image, mask_image, control_image, num_steps, negative_prompt)
    end = time.time()-start
    print("Inference time({}its): {} s".format(num_steps,end))
    
    for i in range(len(results)):
        results[i].save("../result"+str(i)+".png")
