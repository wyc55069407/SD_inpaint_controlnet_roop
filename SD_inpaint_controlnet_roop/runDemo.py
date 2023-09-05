#!/usr/bin/env python3

from run_roop import run_test

orignal_img = "../orignal.png"
mask_img = "../mask.png"
face_img = "../ref_face.png"

prompt = ["a cute child with blue NFL hat","a handsome man with ray-ban sunglasses"]
negative_prompt = ["monochrome, lowres, bad anatomy, worst quality, low quality","monochrome, lowres, bad anatomy, worst quality, low quality"]

if __name__ == '__main__':
    run_test(orignal_img, mask_img, face_img, prompt, negative_prompt)
