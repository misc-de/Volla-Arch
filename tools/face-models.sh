#!/bin/bash
# Converts the face models of OpenCV Zoo that are only published as ONNX to TensorFlow Lite for
# plinius-face: YuNet (finds the face and five points) and SFace (embedding). SFace is too large
# for the repository (38 MB), so both go next to the PKGBUILD (ignored by git), whose checksums
# pin them. With the tool versions below the conversion is reproducible bit for bit.
#
# Needs uv on the host; the Python environment goes to build/face-models-venv.
#
# Usage: tools/face-models.sh
set -euo pipefail
. "$(dirname "$0")/lib.sh"

out=$PKGBUILDS/plinius-face
venv=$BUILD/face-models-venv
work=$BUILD/face-models
# OpenCV Zoo at a fixed commit (the model files are in Git LFS)
zoo=https://media.githubusercontent.com/media/opencv/opencv_zoo/48b10fa505aeb51fbd127fc60a5c660040e823b7/models
# name, path in the zoo, sha256 of the ONNX file
models=(
	"face_detection_yunet_2023mar face_detection_yunet 8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
	"face_recognition_sface_2021dec face_recognition_sface 0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"
)

mkdir -p "$work"
if [[ ! -x $venv/bin/python ]]; then
	msg "Creating the conversion environment"
	uv venv -q --python 3.12 "$venv"
	UV_HTTP_TIMEOUT=600 VIRTUAL_ENV=$venv uv pip install -q \
		tensorflow==2.19.1 tf-keras==2.19.0 keras==3.15.1 numpy==2.1.3 protobuf==5.29.6 \
		ml-dtypes==0.6.0 onnx==1.17.0 onnx2tf==1.28.8 onnx-graphsurgeon==0.6.1 \
		sng4onnx==2.0.1 psutil==7.2.2 onnxruntime==1.31.1 ai-edge-litert==2.3.0
fi

for model in "${models[@]}"; do
	read -r name dir sum <<< "$model"
	onnx=$work/$name.onnx
	if ! echo "$sum  $onnx" | sha256sum -c --status 2>/dev/null; then
		msg "Downloading $name"
		curl -fL -A "$USER_AGENT" -o "$onnx.part" "$zoo/$dir/$name.onnx"
		echo "$sum  $onnx.part" | sha256sum -c --status || die "$name: checksum mismatch"
		mv "$onnx.part" "$onnx"
	fi

	msg "Converting $name to TensorFlow Lite"
	rm -rf "$work/$name" "$work/input"
	# onnx2tf overwrites its input with a simplified model; it gets a copy
	mkdir -p "$work/input" && cp "$onnx" "$work/input/"
	# onnx2tf checks its result with a test image it downloads as a pickled numpy file; random
	# data does that job without loading pickles from the network
	(cd "$work" && "$venv/bin/python" -I - "$work/input/$name.onnx" "$work/$name") <<'EOF'
import sys
import numpy as np
import onnx2tf.onnx2tf as o2t

o2t.download_test_image_data = lambda: np.random.default_rng(0).random((20, 128, 128, 3), dtype=np.float32)
o2t.convert(input_onnx_file_path=sys.argv[1], output_folder_path=sys.argv[2], non_verbose=True,
            copy_onnx_input_output_names_to_tflite=True)
EOF
	install -Dm644 "$work/$name/${name}_float32.tflite" -t "$out"
	sha256sum "$out/${name}_float32.tflite"
done
