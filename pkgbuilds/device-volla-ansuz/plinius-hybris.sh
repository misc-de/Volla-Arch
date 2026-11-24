# libhybris on the Plinius (Halium 14), as on Ubuntu Touch; see /usr/lib/environment.d/50-plinius-hybris.conf
export HYBRIS_LD_LIBRARY_PATH=/system/lib64:/apex/com.android.vndk.v34/lib64:/vendor/lib64/hw
export GLIBC_TUNABLES=glibc.rtld.optional_static_tls=4096
export __EGL_VENDOR_LIBRARY_FILENAMES=/usr/share/glvnd/egl_vendor.d/10_libhybris.json
