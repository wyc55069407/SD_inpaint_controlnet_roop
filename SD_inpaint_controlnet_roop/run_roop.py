#!/usr/bin/env python3

import os

from pipe_gpu_inpaint import sd_inpaint

def run_roop_with_ref(face_img):
    os.system("python run.py -s " + face_img + " -t ../result0.png -o ..\\output0.png --execution-provider openvino")
    os.system("python run.py -s " + face_img + " -t ../result1.png -o ..\\output1.png --execution-provider openvino")

def run_test(orignal_img, mask_img, face_img, prompt, negative_prompt):
    sd_inpaint(orignal_img, mask_img, prompt, negative_prompt)
    run_roop_with_ref(face_img)