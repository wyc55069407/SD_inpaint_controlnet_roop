# Stable Diffusion Inpaint Controlnet Pipeline + Roop

This proj is a demo to run SD1.5 with inpaint controlnet integrated on OpenVino.

## Installation Basic Dependencies

Python
```
winget install -e --id Python.Python.3.10
```
PIP
```
python -m ensurepip
```
GIT
```
winget install -e --id Git.Git
```

## Installation Stable Diffusion Inpaint Controlnet Pipeline on OpenVino

Environment preparation
First, please follow below method to prepare your development environment, you can choose download model from HuggingFace for better runtime experience. In this case, we choose controlNet for inpaint task.

```
$ pip install opencv-contrib-python
$ pip install -q "diffusers>=0.14.0" "git+https://github.com/huggingface/accelerate.git" controlnet-aux gradio
$ pip install openvino openvino-dev onnx
$ pip install torch==1.13.1 #important

$ git lfs install
$ git clone https://huggingface.co/lllyasviel/control_v11p_sd15_inpaint
$ git clone https://huggingface.co/runwayml/stable-diffusion-v1-5
$ git clone https://huggingface.co/openai/clip-vit-large-patch14 
```
Please note, the diffusers start to use `torch.nn.functional.scaled_dot_product_attention` if your installed torch version is >= 2.0, and the ONNX does not support op conversion for “Aten:: scaled_dot_product_attention”. To avoid the error during the model conversion by “torch.onnx.export”, please make sure you are using torch==1.13.1.

## Installation Roop on OpenVino

FFmpeg
```
winget install -e --id Gyan.FFmpeg
```
Reboot your system in order for FFmpeg to function properly.
```
shutdown /r
```
Toolset
Microsoft Visual C++ 2015 Redistributable
```
winget install -e --id Microsoft.VCRedist.2015+.x64
```
Microsoft Visual Studio 2022 build tools
During installation, ensure to select the Desktop Development with C++ package.
```
winget install -e --id Microsoft.VisualStudio.2022.BuildTools --override "--wait --add Microsoft.VisualStudio.Workload.NativeDesktop --includeRecommended"
```

Install dependencies
We highly recommend to work with a venv or conda to avoid issues.

```
cd SD_inpaint_controlnet_roop
pip install -r requirements.txt
```

OpenVINO (Intel)
To make sure running into openvino, modify the env:
```
pip uninstall onnxruntime onnxruntime-openvino
pip install onnxruntime-openvino==1.15.0
```
Generally, this demo is based on https://github.com/s0md3v/roop and modified to running Openvino properly, some of the code are modified:
1. Set provider_options = [{'device_type': 'GPU_FP32'}] in face_swapper.py to force running with Openvino GPU
2. Correct the model download path in face_swapper.py
3. In core.py, add add_openvino_libs_to_path to avoid OV module not found issue.

## Usage

1. Model Conversion
This step convert the orignal models to OpenVino optimized IR format and can run with Intel GPU acceleration.
```
$ python get_model_inpaint.py -sd stable-diffusion-v1-5 -b 2
```
Note: above convertion script needs to set proper path of stable-diffusion-v1-5 in params. Also, the working directory should contain model control_v11p_sd15_inpaint and clip-vit-large-patch14. Pls modify get_model_inpaint.py code properly for control_v11p_sd15_inpaint and clip-vit-large-patch14 path.
In example, batch number is 2 for the converted models.

After running convertion. Please check your current path, make sure you already generated below models currently. Other ONNX files can be deleted for saving space.

controlnet-canny.<xml|bin>
text_encoder.<xml|bin>
unet_controlnet.<xml|bin>
vae_decoder.<xml|bin>
vae_encoder.<xml|bin>
* If your local path already exists ONNX or IR model, the script will jump tore-generate ONNX/IR. If you updated the pytorch model or want to generate model with different shape, please remember to delete existed ONNX and IR models.

2. Runtime pipeline test for Stable Diffusion Inpaint Controlnet Pipeline + Roop
Prepare 512x512 size images:
  1. orignal image. (orignal.png in repo)
  2. mask image. white part is the position to inpaint, black is to keep. (mask.png in repo)
  3. reference face image. Used for roop to replace face. (ref_face.png in repo)
Note: in this repo, above image examples are provided.

Start running pipeline:

```
python runDemo.py
```
Modify runDemo.py to change the path of input images. And setup prompts for drawing in inpaint area:
```
orignal_img = "../orignal.png"
mask_img = "../mask.png"
face_img = "../ref_face.png"
prompt = ["a forest in the night","a nice castle"]
negative_prompt = ["monochrome, lowres, bad anatomy, worst quality, low quality","monochrome, lowres, bad anatomy, worst quality, low quality"]
```

With running the pipeline. OpenVino with Intel GPU accelerate will get orig and mask image and running SD pipeline with inpaint control net to re-draw the position mask indicated and keep the rest in orig. Then using roop (also with OpenVino & Intel GPU) to do swap face according to ref_face.

After executing runDemo.py, following file will be generated:
sd_result0.png sd_result1.png  (SD+inpaint results)
roop_output0.png roop_output1.png   (roop results based on sd_result)

## Disclaimer

This software is designed to contribute positively to the AI-generated media industry, assisting artists with tasks like character animation and models for clothing.

We are aware of the potential ethical issues and have implemented measures to prevent the software from being used for inappropriate content, such as nudity.

Users are expected to follow local laws and use the software responsibly. If using real faces, get consent and clearly label deepfakes when sharing. The developers aren't liable for user actions.


## Licenses

Our software uses a lot of third party libraries as well pre-trained models. The users should keep in mind that these third party components have their own license and terms, therefore our license is not being applied.


## Credits

- [deepinsight](https://github.com/deepinsight) for their [insightface](https://github.com/deepinsight/insightface) project which provided a well-made library and models.
- all developers behind the libraries used in this project
