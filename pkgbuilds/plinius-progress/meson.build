# SPDX-License-Identifier: MIT
# Built as a subdirectory of the BuffyBox source tree (see PKGBUILD). prepare() puts unl0kr's
# lv_conf.h (with the bar widget enabled) and keyboard layouts next to it: sq2lv.h and LVGL
# look for them in the program's own directory.

plinius_progress_dependencies = [
    common_dependencies,
    depxkbcommon
]

plinius_progress_args = []

foreach backend : [['framebuffer', 'LV_USE_LINUX_FBDEV'], ['drm', 'LV_USE_LINUX_DRM'], ['sdl', 'LV_USE_SDL']]
    if get_option('lvgl_backends').contains(backend[0])
        plinius_progress_args += '-D@0@=1'.format(backend[1])
    else
        plinius_progress_args += '-D@0@=0'.format(backend[1])
    endif
endforeach

if get_option('lvgl_backends').contains('drm')
    plinius_progress_dependencies += dependency('libdrm')
endif
if get_option('lvgl_backends').contains('sdl')
    plinius_progress_dependencies += dependency('sdl2')
endif

# unl0kr's config parser for the shared configuration
executable('plinius-progress',
    include_directories: common_include_dirs,
    sources: files('plinius-progress.c', 'font_title.c', 'sq2lv_layouts.c', '../unl0kr/config.c')
        + shared_sources + shared_sources_ul_f0 + squeek2lvgl_sources + lvgl_sources,
    dependencies: plinius_progress_dependencies,
    c_args: plinius_progress_args,
    install: true,
    install_tag: 'plinius-progress'
)
