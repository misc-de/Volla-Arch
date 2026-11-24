#!/usr/bin/env python3
# Background for the glass theme of unl0kr and plinius-progress: the Phosh wallpaper
# (GNOME "blobs", dark variant), cut to a portrait phone screen like Phosh's "zoom", blurred
# and darkened for readable text, stored small: the programs scale it to the screen once at
# start. Output is an LVGL binary image (lv_image_header_t + XRGB8888 pixels).
# Usage: glass-background.py WALLPAPER.svg OUT.bin [PREVIEW.png]
# Needs rsvg-convert (librsvg) and Pillow.

import io
import struct
import subprocess
import sys

from PIL import Image, ImageEnhance, ImageFilter

RENDER = 1024            # SVG rendered at this size (square wallpaper)
ASPECT = 9 / 20          # portrait phone screen (Volla: 1080x2400)
OUT_W, OUT_H = 216, 480  # stored size; blurred, so upscaling loses nothing visible
BLUR = 8                 # Gaussian radius in rendered pixels
BRIGHTNESS = 0.85        # darken a little so white text stays readable
SATURATION = 1.2         # keep the colours vivid behind the glass

LV_IMAGE_HEADER_MAGIC = 0x19
LV_COLOR_FORMAT_XRGB8888 = 0x11

svg, out = sys.argv[1], sys.argv[2]
png = subprocess.run(["rsvg-convert", "-w", str(RENDER), "-h", str(RENDER), svg],
                     check=True, capture_output=True).stdout
image = Image.open(io.BytesIO(png)).convert("RGB")

# Centre crop to the screen's aspect ratio, as Phosh does
crop_w = round(RENDER * ASPECT)
left = (RENDER - crop_w) // 2
image = image.crop((left, 0, left + crop_w, RENDER))
image = image.filter(ImageFilter.GaussianBlur(BLUR))
image = ImageEnhance.Brightness(image).enhance(BRIGHTNESS)
image = ImageEnhance.Color(image).enhance(SATURATION)
image = image.resize((OUT_W, OUT_H), Image.LANCZOS)

# lv_image_header_t: magic, cf, flags (8/8/16 bit), w, h (16/16), stride, reserved (16/16)
header = struct.pack("<BBHHHHH", LV_IMAGE_HEADER_MAGIC, LV_COLOR_FORMAT_XRGB8888, 0,
                     OUT_W, OUT_H, OUT_W * 4, 0)
# XRGB8888 in memory order B, G, R, X (little endian)
pixels = image.convert("RGBX").tobytes("raw", "BGRX")
with open(out, "wb") as f:
    f.write(header + pixels)

if len(sys.argv) > 3:
    image.save(sys.argv[3])
