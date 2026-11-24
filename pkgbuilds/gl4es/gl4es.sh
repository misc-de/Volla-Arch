#!/bin/bash
# gl4es <program> [arguments...] (installed as /usr/bin/gl4es): runs a program with desktop OpenGL (up to 2.1) on the phone's
# GLES driver. The program finds gl4es' libGL and its EGL wrapper first; gl4es itself draws
# through the real EGL and GLES libraries (glvnd, which takes the Android driver through
# libhybris). Without it, a program that asks for desktop OpenGL gets no context at all: the
# Mali driver offers OpenGL ES only.
set -euo pipefail

if (($# == 0)); then
	echo "usage: gl4es <program> [arguments...]" >&2
	exit 2
fi

dir=${GL4ES_DIR:-/usr/lib/gl4es}
export LD_LIBRARY_PATH="$dir${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
# gl4es would otherwise find its own wrapper as libEGL through LD_LIBRARY_PATH
export LIBGL_EGL=${LIBGL_EGL:-/usr/lib/libEGL.so.1}
export LIBGL_GLES=${LIBGL_GLES:-/usr/lib/libGLESv2.so.2}
# The banner lists the driver's limits on every start; LIBGL_NOBANNER=0 shows it again
export LIBGL_NOBANNER=${LIBGL_NOBANNER:-1}
exec "$@"
