/*
 * plinius-progress: full-screen status view for the initramfs, in the look of unl0kr
 *
 * Shows a title and a message, optionally with
 *   -p  a progress bar fed by "cryptsetup ... --progress-json" on stdin (exits at EOF), or
 *   -b  a button that the user confirms (exits when pressed or after -T seconds), and with
 *       -B a second one below it for the alternative (exit status 2).
 * Without -p or -b the view stays until the process is terminated.
 *
 * With -F (and -b) a question at the first start of the system takes the look of the system's own
 * first question, the PIN prompt of plinius-greeter: black, the title as a heading in Adwaita Sans
 * Bold (bbx_font_title) with the message below it in subdued white, and at the bottom the default
 * answer as a pill in the accent color with the alternative below it as plain text.
 *
 * With -l it shows nothing but the boot logo of the system on black, the picture that phoc
 * shows when the session starts (phoc-plinius patches 0004/0007): the mark turns like a coin
 * above the wordmark, both in the accent color. Started once the disk is open, it fills the
 * time until the session's compositor takes over the display (exits on SIGTERM or after -T
 * seconds). With -H, SIGUSR1 hands the display over without a dark moment: the mark turns to
 * the front (where phoc's turn starts), the program gives up DRM master but keeps its last
 * picture on the screen, renames itself to "logo-handed" (its comm, for whoever sent the
 * signal) and exits once the compositor shows its own frame. No files are involved: started in
 * the ramdisk, the program outlives the switch to the system, and then it sees neither the
 * system's /run nor any /proc. With -L it then sets the backlight again through that sysfs file,
 * opened at the start: the compositor's modeset leaves the panel dark otherwise.
 *
 * Built inside the BuffyBox source tree: display, input and theme come from its shared code,
 * and the unl0kr configuration (/etc/unl0kr.conf.d etc.) selects backend, theme, background
 * and quirks, so both programs look the same. Texts come from the caller, already translated.
 *
 * SPDX-License-Identifier: MIT
 */

#include "../unl0kr/config.h"

#include "../shared/background.h"
#include "../shared/backends.h"
#include "../shared/cli_common.h"
#include "../shared/display.h"
#include "../shared/indev.h"
#include "../shared/log.h"
#include "../shared/terminal.h"
#include "../shared/theme.h"
#include "../shared/themes.h"

#include "lvgl/lvgl.h"

#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/prctl.h>
#include <time.h>
#include <unistd.h>

#if LV_USE_LINUX_DRM
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <xf86drm.h>
#include <xf86drmMode.h>
#endif

#ifndef PROJECT_VERSION
#define PROJECT_VERSION "?"
#endif

/* Breeze "warning" orange; the BuffyBox themes have no warning color */
#define WARNING_COLOR 0xf67400

/* Boot logo (-l), the same values as phoc's: */
/* One turn of the mark takes this long (two spin cycles of 1.2 s in phoc) */
#define LOGO_TURN_MS 2400
/* Opacity of the mark when seen edge-on: it looks darker while turned away */
#define LOGO_EDGE_OPA 140
/* The step below the logo (-m), as phoc-plinius 0028 draws it: the gap above it in units of the
 * logo's width (220 in phoc), subdued white */
#define LOGO_TEXT_GAP (12.0 / 220)
#define LOGO_TEXT_OPA LV_OPA_60
/* After the handover the last picture stays at most this long, should no compositor take over */
#define HANDOVER_MAX_MS 30000

/* The first-start look (-F), from plinius-greeter's CSS: the subtitle in white at 62 %, the
 * alternative answer at 80 % */
#define FIRST_MESSAGE_OPA 158
#define FIRST_ALT_OPA LV_OPA_80

/* Adwaita Sans Bold at 44 px (font_title.c), the heading of -F */
LV_FONT_DECLARE(bbx_font_title);

typedef struct {
    bbx_cli_common_opts common;
    const char **config_files;
    int num_config_files;
    const char *title;
    const char *message;
    const char *icon;
    const char *remaining;
    const char *button;
    const char *button2;
    const char *logo;
    const char *backlight;
    int timeout;
    bool handover;
    bool progress;
    bool first_start;
} pp_cli_opts;

static pp_cli_opts cli_opts;
static ul_config_opts conf_opts;

static lv_obj_t *bar;
static lv_obj_t *percent_label;
/* Images of the boot logo (-l); referenced by the widgets, so they live as long as the program */
static lv_image_dsc_t logo_mark;
static lv_image_dsc_t logo_wordmark;
static lv_obj_t *logo_mark_obj;
/* The backlight's brightness file (-L), opened while it can still be reached; -1 without */
static int backlight_fd = -1;
/* Set by SIGUSR1: hand the display over (-H) */
static volatile sig_atomic_t handover_requested;
/* Accent color of the theme (that of the progress bar), for focus and default button */
static uint32_t theme_accent;

/* Current line from stdin; overlong lines are dropped */
static char line_buf[1024];
static size_t line_len;
static bool line_overflow;

static void print_usage(void) {
    fprintf(stderr,
        "Usage: plinius-progress [OPTION]\n"
        "\n"
        "  -t, --title=TEXT          Title\n"
        "  -m, --message=TEXT        Message below the title\n"
        "  -i, --icon=NAME           Icon above the title: warning, ok\n"
        "  -p, --progress            Show a progress bar fed by cryptsetup\n"
        "                            --progress-json on stdin; exit at EOF\n"
        "  -r, --remaining=TEXT      Remaining time next to the percentage, %%s is\n"
        "                            replaced by the minutes\n"
        "  -b, --button=TEXT         Show a button and exit when it is pressed\n"
        "  -B, --button2=TEXT        With -b: second button below it, exits with\n"
        "                            status 2\n"
        "  -T, --timeout=SECONDS     With -b or -l: exit after this many seconds\n"
        "  -F, --first-start         With -b: the look of the first start, black\n"
        "                            with a heading, the button a pill in the\n"
        "                            accent color at the bottom\n"
        "  -l, --logo=DIR            Show only the boot logo: DIR/mark.bin right\n"
        "                            above DIR/wordmark.bin (LVGL ARGB8888 images\n"
        "                            at their size on the screen), -m below them as\n"
        "                            the step of the start; exit on SIGTERM\n"
        "  -H, --handover            With -l: on SIGUSR1 give up the display but keep\n"
        "                            the last picture until a compositor replaces\n"
        "                            it; the process is then named logo-handed\n"
        "  -L, --backlight=FILE      With -H: set this brightness file again once the\n"
        "                            compositor has taken over\n"
        "  -C, --config-override     Additional unl0kr config file (repeatable)\n"
        "  -g, --geometry=NxM[@X,Y]  Force display size and offset\n"
        "  -d, --dpi=N               Override the display's DPI value\n"
        "  -v, --verbose             More logging on stderr\n"
        "  -h, --help                Print this message and exit\n"
        "  -V, --version             Print the version and exit\n");
}

/* Parse the command line into cli_opts; exits on errors */
static void parse_opts(int argc, char *argv[]) {
    bbx_cli_init_common_opts(&cli_opts.common);

    struct option long_opts[] = {
        { "title",           required_argument, NULL, 't' },
        { "message",         required_argument, NULL, 'm' },
        { "icon",            required_argument, NULL, 'i' },
        { "progress",        no_argument,       NULL, 'p' },
        { "remaining",       required_argument, NULL, 'r' },
        { "button",          required_argument, NULL, 'b' },
        { "button2",         required_argument, NULL, 'B' },
        { "timeout",         required_argument, NULL, 'T' },
        { "first-start",     no_argument,       NULL, 'F' },
        { "logo",            required_argument, NULL, 'l' },
        { "handover",        no_argument,       NULL, 'H' },
        { "backlight",       required_argument, NULL, 'L' },
        { "config-override", required_argument, NULL, 'C' },
        { "geometry",        required_argument, NULL, 'g' },
        { "dpi",             required_argument, NULL, 'd' },
        { "verbose",         no_argument,       NULL, 'v' },
        { "help",            no_argument,       NULL, 'h' },
        { "version",         no_argument,       NULL, 'V' },
        { NULL, 0, NULL, 0 }
    };

    int opt;
    while ((opt = getopt_long(argc, argv, "t:m:i:pr:b:B:T:Fl:HL:C:g:d:vhV", long_opts, NULL)) != -1) {
        switch (opt) {
        case 't': cli_opts.title = optarg; break;
        case 'm': cli_opts.message = optarg; break;
        case 'i': cli_opts.icon = optarg; break;
        case 'p': cli_opts.progress = true; break;
        case 'r': cli_opts.remaining = optarg; break;
        case 'b': cli_opts.button = optarg; break;
        case 'B': cli_opts.button2 = optarg; break;
        case 'T': cli_opts.timeout = atoi(optarg); break;
        case 'F': cli_opts.first_start = true; break;
        case 'l': cli_opts.logo = optarg; break;
        case 'H': cli_opts.handover = true; break;
        case 'L': cli_opts.backlight = optarg; break;
        case 'C':
            cli_opts.config_files = realloc(cli_opts.config_files,
                (cli_opts.num_config_files + 1) * sizeof(char *));
            if (!cli_opts.config_files) {
                bbx_log(BBX_LOG_LEVEL_ERROR, "Could not allocate memory for config file paths");
                exit(EXIT_FAILURE);
            }
            cli_opts.config_files[cli_opts.num_config_files++] = optarg;
            break;
        case 'g':
            if (bbx_cli_parse_geometry(optarg, &cli_opts.common) != 0)
                exit(EXIT_FAILURE);
            break;
        case 'd':
            if (bbx_cli_parse_dpi(optarg, &cli_opts.common) != 0)
                exit(EXIT_FAILURE);
            break;
        case 'v': cli_opts.common.verbose = true; break;
        case 'h': print_usage(); exit(EXIT_SUCCESS);
        case 'V': bbx_cli_print_version_and_exit("plinius-progress"); break;
        default: print_usage(); exit(EXIT_FAILURE);
        }
    }
}

/* Restore the terminal and exit with status 0 (also the handler for SIGTERM/SIGINT) */
static void exit_success(int signum) {
    LV_UNUSED(signum);
    bbx_terminal_reset_current_terminal();
    exit(EXIT_SUCCESS);
}

/* Restore the terminal and exit with status 2: the second button was chosen */
static void exit_alternative(void) {
    bbx_terminal_reset_current_terminal();
    exit(2);
}

/* Restore the terminal and exit with a failure status, so the caller can fall back */
static void exit_failure(void) {
    bbx_terminal_reset_current_terminal();
    exit(EXIT_FAILURE);
}

static void button_clicked_cb(lv_event_t *event) {
    LV_UNUSED(event);
    exit_success(0);
}

static void button2_clicked_cb(lv_event_t *event) {
    LV_UNUSED(event);
    exit_alternative();
}

static void timeout_cb(lv_timer_t *timer) {
    LV_UNUSED(timer);
    exit_success(0);
}

/* Value of the quoted number field "key":"123" in a cryptsetup progress line */
static bool json_number(const char *line, const char *key, uint64_t *value) {
    char pattern[64];
    snprintf(pattern, sizeof(pattern), "\"%s\":\"", key);
    const char *p = strstr(line, pattern);
    if (!p)
        return false;
    *value = strtoull(p + strlen(pattern), NULL, 10);
    return true;
}

/* Copy template into out with the first "%s" replaced by number (no printf format from outside) */
static void fill_template(char *out, size_t size, const char *template, unsigned number) {
    const char *p = strstr(template, "%s");
    if (!p) {
        snprintf(out, size, "%s", template);
        return;
    }
    snprintf(out, size, "%.*s%u%s", (int)(p - template), template, number, p + 2);
}

/* Update bar and labels from one line of cryptsetup --progress-json; other lines are ignored */
static void handle_line(const char *line) {
    uint64_t done, size, eta_ms = 0;
    if (!json_number(line, "device_bytes", &done) || !json_number(line, "device_size", &size) || size == 0)
        return;
    json_number(line, "eta_ms", &eta_ms);

    int32_t permille = (int32_t)(done * 1000 / size);
    lv_bar_set_value(bar, permille, conf_opts.general.animations ? LV_ANIM_ON : LV_ANIM_OFF);

    char text[256];
    int n = snprintf(text, sizeof(text), "%" PRId32 " %%", permille / 10);
    if (cli_opts.remaining && eta_ms > 0 && n > 0 && (size_t)n < sizeof(text)) {
        char remaining[192];
        fill_template(remaining, sizeof(remaining), cli_opts.remaining, (unsigned)((eta_ms + 59999) / 60000));
        snprintf(text + n, sizeof(text) - n, "  ·  %s", remaining);
    }
    lv_label_set_text(percent_label, text);
}

/* epoll handler for stdin: split into lines; exits at EOF */
static void on_stdin(void) {
    char buf[512];
    ssize_t n = read(STDIN_FILENO, buf, sizeof(buf));
    if (n < 0 && (errno == EAGAIN || errno == EINTR))
        return;
    if (n <= 0) {
        lv_refr_now(NULL);
        exit_success(0);
    }

    for (ssize_t i = 0; i < n; i++) {
        if (buf[i] == '\n') {
            line_buf[line_len] = '\0';
            if (!line_overflow)
                handle_line(line_buf);
            line_len = 0;
            line_overflow = false;
        } else if (line_len < sizeof(line_buf) - 1) {
            line_buf[line_len++] = buf[i];
        } else {
            line_overflow = true;
        }
    }
}

/* Centered, wrapping label with text on parent */
static lv_obj_t *add_label(lv_obj_t *parent, const char *text) {
    lv_obj_t *label = lv_label_create(parent);
    lv_obj_set_width(label, LV_PCT(100));
    lv_label_set_long_mode(label, LV_LABEL_LONG_MODE_WRAP);
    lv_obj_set_style_text_align(label, LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);
    lv_label_set_text(label, text);
    return label;
}

/* Button with text on parent that calls cb when clicked; Enter on a hardware keyboard presses
 * the focused button, Tab moves the focus */
static lv_obj_t *add_button(lv_obj_t *parent, const char *text, lv_event_cb_t cb, int32_t padding) {
    lv_obj_t *button = lv_button_create(parent);
    lv_obj_set_style_pad_hor(button, padding, LV_PART_MAIN);
    lv_obj_set_style_pad_ver(button, padding / 2, LV_PART_MAIN);
    lv_obj_t *label = lv_label_create(button);
    lv_label_set_text(label, text);
    lv_obj_center(label);
    lv_obj_add_event_cb(button, cb, LV_EVENT_CLICKED, NULL);
    lv_group_add_obj(keyboard_input_group, button);
    /* The shared theme marks no keyboard focus; an outline shows which button Enter presses */
    lv_obj_set_style_outline_width(button, lv_dpx(3), LV_STATE_FOCUS_KEY);
    lv_obj_set_style_outline_pad(button, lv_dpx(3), LV_STATE_FOCUS_KEY);
    lv_obj_set_style_outline_color(button, lv_color_hex(theme_accent), LV_STATE_FOCUS_KEY);
    return button;
}

/* Load the logo image DIR/NAME (lv_image_header_t + ARGB8888 pixels) into dsc and tint it like
 * phoc does: every pixel is white mixed with the logo's own blue by its red channel, and the
 * same mix is made with the accent color. White parts stay white, blue ones take the accent. */
static bool load_logo_image(const char *dir, const char *name, uint32_t accent, lv_image_dsc_t *dsc) {
    char path[PATH_MAX];
    snprintf(path, sizeof(path), "%s/%s", dir, name);

    FILE *file = fopen(path, "rb");
    if (!file) {
        bbx_log(BBX_LOG_LEVEL_ERROR, "Cannot open %s", path);
        return false;
    }
    lv_image_header_t header;
    bool valid = fread(&header, sizeof(header), 1, file) == 1
        && header.magic == LV_IMAGE_HEADER_MAGIC
        && header.cf == LV_COLOR_FORMAT_ARGB8888
        && header.w > 0 && header.h > 0
        && header.stride >= header.w * 4;
    uint8_t *data = NULL;
    size_t size = 0;
    if (valid) {
        size = (size_t)header.stride * header.h;
        data = malloc(size);
        valid = data && fread(data, 1, size, file) == size;
    }
    fclose(file);
    if (!valid) {
        bbx_log(BBX_LOG_LEVEL_ERROR, "%s is not an ARGB8888 LVGL image", path);
        free(data);
        return false;
    }

    const uint32_t ar = (accent >> 16) & 0xff, ag = (accent >> 8) & 0xff, ab = accent & 0xff;
    for (uint32_t y = 0; y < header.h; y++) {
        /* LVGL keeps ARGB8888 as B, G, R, A in memory, not premultiplied */
        uint8_t *px = data + (size_t)y * header.stride;
        for (uint32_t x = 0; x < header.w; x++, px += 4) {
            if (px[3] == 0)
                continue;
            /* Share of blue, 0..255, by how far red falls short of white */
            const uint32_t blue = 255 - px[2];
            px[2] = (uint8_t)(255 - blue * (255 - ar) / 255);
            px[1] = (uint8_t)(255 - blue * (255 - ag) / 255);
            px[0] = (uint8_t)(255 - blue * (255 - ab) / 255);
        }
    }

    memset(dsc, 0, sizeof(*dsc));
    dsc->header = header;
    dsc->data_size = (uint32_t)size;
    dsc->data = data;
    return true;
}

/* Animation step: angle in degrees of the mark's turn around its vertical axis */
static void logo_turn_cb(void *mark, int32_t angle) {
    const int32_t turn = LV_ABS(lv_trigo_cos((int16_t)angle));
    /* Seen edge-on it would vanish; keep a sliver, as phoc does */
    const int32_t scale = LV_MAX(turn * LV_SCALE_NONE / LV_TRIGO_SIN_MAX, 3);
    lv_image_set_scale_x(mark, (uint32_t)scale);
    lv_obj_set_style_image_opa(mark,
        (lv_opa_t)(LOGO_EDGE_OPA + (LV_OPA_COVER - LOGO_EDGE_OPA) * turn / LV_TRIGO_SIN_MAX),
        LV_PART_MAIN);
}

/* Build the boot logo view: mark above the wordmark, centered on black. Exits if the images
 * cannot be read: the caller then just has no logo. */
static void build_logo(uint32_t accent) {
    if (!load_logo_image(cli_opts.logo, "mark.bin", accent, &logo_mark)
            || !load_logo_image(cli_opts.logo, "wordmark.bin", accent, &logo_wordmark))
        exit_failure();

    lv_obj_t *screen = lv_screen_active();
    lv_obj_remove_flag(screen, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_bg_color(screen, lv_color_black(), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(screen, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_flex_flow(screen, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(screen, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    /* The gap between the two is part of the wordmark image (tools/crypt-ramdisk.sh) */
    lv_obj_set_style_pad_row(screen, 0, LV_PART_MAIN);
    lv_obj_set_style_pad_all(screen, 0, LV_PART_MAIN);

    lv_obj_t *mark = lv_image_create(screen);
    lv_image_set_src(mark, &logo_mark);
    logo_mark_obj = mark;
    lv_obj_t *wordmark = lv_image_create(screen);
    lv_image_set_src(wordmark, &logo_wordmark);

    /* The step, below the wordmark but outside the layout, so that the logo stays where phoc
     * has its own (the wordmark is as wide as phoc's logo box) */
    if (cli_opts.message) {
        lv_obj_t *step = lv_label_create(screen);
        lv_obj_add_flag(step, LV_OBJ_FLAG_FLOATING);
        lv_label_set_text(step, cli_opts.message);
        lv_obj_set_style_text_color(step, lv_color_white(), LV_PART_MAIN);
        lv_obj_set_style_text_opa(step, LOGO_TEXT_OPA, LV_PART_MAIN);
        lv_obj_update_layout(screen);
        lv_obj_align_to(step, wordmark, LV_ALIGN_OUT_BOTTOM_MID, 0,
            (int32_t)(logo_wordmark.header.w * LOGO_TEXT_GAP + 0.5));
    }

    lv_anim_t anim;
    lv_anim_init(&anim);
    lv_anim_set_var(&anim, mark);
    lv_anim_set_exec_cb(&anim, logo_turn_cb);
    lv_anim_set_values(&anim, 0, 359);
    lv_anim_set_duration(&anim, LOGO_TURN_MS);
    lv_anim_set_repeat_count(&anim, LV_ANIM_REPEAT_INFINITE);
    lv_anim_start(&anim);
}

static void request_handover(int signum) {
    LV_UNUSED(signum);
    handover_requested = 1;
}

#if LV_USE_LINUX_DRM
/* Major number of DRM devices; minors from 128 on are render nodes, which have no master */
#define DRM_MAJOR 226

/* Our file descriptor of a DRM card (the display backend's), or -1. Found by trying the
 * descriptors themselves: after the switch to the system there is no /proc to list them, and no
 * /dev/dri path that would match (07.11.: "no DRM device" on the phone, twice) */
static int find_drm_fd(void) {
    for (int fd = 0; fd < 1024; fd++) {
        struct stat st;
        if (fstat(fd, &st) == 0 && S_ISCHR(st.st_mode)
                && major(st.st_rdev) == DRM_MAJOR && minor(st.st_rdev) < 128)
            return fd;
    }
    return -1;
}

/* True while some CRTC still scans out a framebuffer of ours (from the list ids/fbs) */
static bool picture_still_ours(int fd, const uint32_t *ids, const uint32_t *fbs, int count) {
    for (int i = 0; i < count; i++) {
        drmModeCrtc *crtc = drmModeGetCrtc(fd, ids[i]);
        if (!crtc)
            continue;
        bool ours = crtc->buffer_id == fbs[i];
        drmModeFreeCrtc(crtc);
        if (ours)
            return true;
    }
    return false;
}
#endif

/* Write the backlight's own value back (-L). The Plinius panel goes dark on the compositor's
 * modeset (phoc picks 120 Hz, the ramdisk ran at 60), and plinius-backlight-wake only sets the
 * backlight again when the display was off before, which it never was here (07.11.: the
 * compositor's logo was drawn but stayed invisible until Phosh set the brightness 14 s later).
 * The LED core skips a write of the value it has, hence one below first. */
static void set_backlight_again(void) {
    char buf[16];
    ssize_t n = pread(backlight_fd, buf, sizeof(buf) - 1, 0);
    if (n <= 0)
        return;
    buf[n] = '\0';
    int value = atoi(buf);
    char tmp[16], cur[16];
    int len_tmp = snprintf(tmp, sizeof(tmp), "%d", value > 0 ? value - 1 : value + 1);
    int len_cur = snprintf(cur, sizeof(cur), "%d", value);
    if (pwrite(backlight_fd, tmp, len_tmp, 0) != len_tmp || pwrite(backlight_fd, cur, len_cur, 0) != len_cur)
        fprintf(stderr, "plinius-progress: cannot set the backlight again: %s\n", strerror(errno));
    else
        fprintf(stderr, "plinius-progress: backlight set again (%d)\n", value);
}

/* Hand the display over to the compositor that comes next (-H, on SIGUSR1); does not return.
 * The mark turns to the front, where phoc's own turn starts. DRM master goes, the picture stays:
 * the CRTC keeps showing our framebuffer as long as we hold it, until the compositor's first
 * commit replaces it. Only then do we close the device; closing earlier would leave the screen
 * dark until then. The terminal keeps its graphics mode: it belongs to the session by now. */
static void hand_over(void) {
#if LV_USE_LINUX_DRM
    if (logo_mark_obj) {
        lv_anim_delete(logo_mark_obj, NULL);
        lv_image_set_scale_x(logo_mark_obj, LV_SCALE_NONE);
        lv_obj_set_style_image_opa(logo_mark_obj, LV_OPA_COVER, LV_PART_MAIN);
    }
    lv_refr_now(NULL);
    /* Let the page flip of that frame complete */
    usleep(50000);

    int fd = find_drm_fd();
    if (fd < 0 || drmDropMaster(fd) != 0) {
        /* Not on DRM or not master: closing the device is all that is left to do. Logged as an
         * error so that it shows at the default level (the caller sends stderr to the kernel log) */
        bbx_log(BBX_LOG_LEVEL_ERROR, "plinius-progress: cannot give up DRM master (fd %d: %s), exiting",
            fd, fd < 0 ? "no DRM device" : strerror(errno));
        _exit(EXIT_SUCCESS);
    }

    uint32_t ids[8], fbs[8];
    int count = 0;
    drmModeRes *res = drmModeGetResources(fd);
    for (int i = 0; res && i < res->count_crtcs && count < 8; i++) {
        drmModeCrtc *crtc = drmModeGetCrtc(fd, res->crtcs[i]);
        if (crtc && crtc->buffer_id) {
            ids[count] = crtc->crtc_id;
            fbs[count++] = crtc->buffer_id;
        }
        drmModeFreeCrtc(crtc);
    }
    drmModeFreeResources(res);

    /* Tell the sender of the signal that the display is free (it reads /proc/<pid>/comm), and
     * report how it went on stderr, which the caller points at the kernel log */
    prctl(PR_SET_NAME, "logo-handed", 0, 0, 0);
    fprintf(stderr, "plinius-progress: display handed over, %d CRTC(s) showing the logo\n", count);

    int waited = 0;
    while (waited < HANDOVER_MAX_MS && picture_still_ours(fd, ids, fbs, count)) {
        usleep(100000);
        waited += 100;
    }
    fprintf(stderr, waited < HANDOVER_MAX_MS ? "plinius-progress: replaced by the compositor after %d ms\n"
        : "plinius-progress: not replaced within %d ms\n", waited);
    if (waited < HANDOVER_MAX_MS && backlight_fd >= 0)
        set_backlight_again();
#endif
    _exit(EXIT_SUCCESS);
}

/* Build the view: icon, title, progress bar with percentage, message, buttons */
/* Button of the first-start look: as wide as the column, round ends, no frame or shadow of the
 * theme; filled with the accent color, or plain text for the alternative */
static void style_first_button(lv_obj_t *button, bool filled) {
    lv_obj_set_width(button, LV_PCT(100));
    lv_obj_set_style_radius(button, LV_RADIUS_CIRCLE, LV_PART_MAIN);
    lv_obj_set_style_border_width(button, 0, LV_PART_MAIN);
    lv_obj_set_style_shadow_width(button, 0, LV_PART_MAIN);
    lv_obj_set_style_text_color(button, lv_color_white(), LV_PART_MAIN);
    lv_obj_set_style_bg_color(button, filled ? lv_color_hex(theme_accent) : lv_color_white(), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(button, filled ? LV_OPA_COVER : LV_OPA_TRANSP, LV_PART_MAIN);
    /* Pressed: the pill gets lighter, the text answer a light disc as the greeter's keys */
    lv_obj_set_style_bg_color(button, filled ? lv_color_lighten(lv_color_hex(theme_accent), LV_OPA_20)
                                             : lv_color_white(), LV_STATE_PRESSED);
    lv_obj_set_style_bg_opa(button, filled ? LV_OPA_COVER : LV_OPA_30, LV_STATE_PRESSED);
    if (!filled)
        lv_obj_set_style_text_opa(button, FIRST_ALT_OPA, LV_PART_MAIN);
}

/* The first-start look (-F): black, heading and message at the top, the answers at the bottom */
static void build_first_start_ui(lv_obj_t *screen, int32_t ver_res, int32_t padding) {
    lv_obj_set_style_bg_color(screen, lv_color_black(), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(screen, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_flex_align(screen, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    /* The heading where the greeter has it below the status bar, the answers above the gesture bar */
    lv_obj_set_style_pad_top(screen, ver_res / 8, LV_PART_MAIN);
    lv_obj_set_style_pad_bottom(screen, padding * 2, LV_PART_MAIN);
    lv_obj_set_style_pad_row(screen, padding / 3, LV_PART_MAIN);

    if (cli_opts.title) {
        lv_obj_t *title = add_label(screen, cli_opts.title);
        lv_obj_set_style_text_font(title, &bbx_font_title, LV_PART_MAIN);
        lv_obj_set_style_text_color(title, lv_color_white(), LV_PART_MAIN);
    }
    if (cli_opts.message) {
        lv_obj_t *message = add_label(screen, cli_opts.message);
        lv_obj_set_style_text_color(message, lv_color_white(), LV_PART_MAIN);
        lv_obj_set_style_text_opa(message, FIRST_MESSAGE_OPA, LV_PART_MAIN);
    }

    /* Everything below goes to the bottom */
    lv_obj_t *space = lv_obj_create(screen);
    lv_obj_remove_style_all(space);
    lv_obj_set_width(space, LV_PCT(100));
    lv_obj_set_flex_grow(space, 1);

    lv_obj_t *button = add_button(screen, cli_opts.button, button_clicked_cb, padding);
    style_first_button(button, true);
    if (cli_opts.button2) {
        lv_obj_t *button2 = add_button(screen, cli_opts.button2, button2_clicked_cb, padding);
        style_first_button(button2, false);
    }
    lv_group_focus_obj(button);
}

static void build_ui(lv_display_t *disp, const bbx_theme *theme) {
    const int32_t hor_res = lv_display_get_horizontal_resolution(disp);
    const int32_t ver_res = lv_display_get_vertical_resolution(disp);
    /* Same side padding as unl0kr: landscape screens get a centered portrait column */
    const int32_t padding = (ver_res > hor_res ? ver_res / 2.5 : ver_res / 1.8) / 10;
    const int32_t side_pad = (hor_res > ver_res ? (hor_res - ver_res) / 2 : 0) + padding;

    theme_accent = theme->bar.indicator.bg_color;

    lv_obj_t *screen = lv_screen_active();
    lv_theme_apply(screen);
    const bool first_look = cli_opts.first_start && cli_opts.button && !cli_opts.progress;
    /* The first-start look is black, without the system's background picture */
    if (conf_opts.theme.background && !first_look) {
        bbx_background_apply(screen, conf_opts.theme.background);
    }
    lv_obj_remove_flag(screen, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_flex_flow(screen, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(screen, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_left(screen, side_pad, LV_PART_MAIN);
    lv_obj_set_style_pad_right(screen, side_pad, LV_PART_MAIN);
    lv_obj_set_style_pad_row(screen, padding, LV_PART_MAIN);

    if (first_look) {
        build_first_start_ui(screen, ver_res, padding);
        return;
    }

    if (cli_opts.icon) {
        bool warning = strcmp(cli_opts.icon, "warning") == 0;
        lv_obj_t *icon = add_label(screen, warning ? LV_SYMBOL_WARNING : LV_SYMBOL_OK);
        lv_obj_set_style_text_color(icon,
            lv_color_hex(warning ? WARNING_COLOR : theme_accent), LV_PART_MAIN);
    }

    if (cli_opts.title)
        add_label(screen, cli_opts.title);

    if (cli_opts.progress) {
        bar = lv_bar_create(screen);
        lv_obj_set_size(bar, LV_PCT(100), lv_dpx(24));
        lv_bar_set_range(bar, 0, 1000);
        /* The shared theme does not style bars; apply its bar colors here */
        /* Track: clear, or a glass surface in a glass theme */
        if (theme->glass.enabled) {
            lv_obj_set_style_bg_color(bar, lv_color_white(), LV_PART_MAIN);
            lv_obj_set_style_bg_opa(bar, theme->glass.surface_opa, LV_PART_MAIN);
        } else {
            lv_obj_set_style_bg_opa(bar, LV_OPA_TRANSP, LV_PART_MAIN);
        }
        lv_obj_set_style_border_width(bar, lv_dpx(theme->bar.border_width), LV_PART_MAIN);
        lv_obj_set_style_border_color(bar, lv_color_hex(theme->bar.border_color), LV_PART_MAIN);
        lv_obj_set_style_radius(bar, lv_dpx(theme->bar.corner_radius), LV_PART_MAIN);
        lv_obj_set_style_pad_all(bar, lv_dpx(3), LV_PART_MAIN);
        lv_obj_set_style_bg_opa(bar, LV_OPA_COVER, LV_PART_INDICATOR);
        lv_obj_set_style_bg_color(bar, lv_color_hex(theme->bar.indicator.bg_color), LV_PART_INDICATOR);
        lv_obj_set_style_radius(bar, lv_dpx(theme->bar.corner_radius), LV_PART_INDICATOR);
        lv_obj_set_style_anim_duration(bar, 800, LV_PART_MAIN);

        percent_label = add_label(screen, "0 %");
    }

    if (cli_opts.message) {
        lv_obj_t *message = add_label(screen, cli_opts.message);
        lv_obj_set_style_text_opa(message, LV_OPA_80, LV_PART_MAIN);
    }

    if (cli_opts.button) {
        lv_obj_t *button = add_button(screen, cli_opts.button, button_clicked_cb, padding);
        if (cli_opts.button2) {
            lv_obj_t *button2 = add_button(screen, cli_opts.button2, button2_clicked_cb, padding);
            /* Two buttons of equal width, the first one filled as the default */
            lv_obj_set_width(button, LV_PCT(100));
            lv_obj_set_width(button2, LV_PCT(100));
            lv_obj_set_style_bg_color(button, lv_color_hex(theme_accent), LV_PART_MAIN);
            lv_obj_set_style_bg_opa(button, LV_OPA_COVER, LV_PART_MAIN);
            lv_obj_set_style_border_color(button, lv_color_hex(theme_accent), LV_PART_MAIN);
        }
        lv_group_focus_obj(button);
    }
}

int main(int argc, char *argv[]) {
    parse_opts(argc, argv);
    if (cli_opts.common.verbose)
        bbx_log_set_level(BBX_LOG_LEVEL_VERBOSE);

    /* Same configuration files as unl0kr, in the same order */
    ul_config_init_opts(&conf_opts);
    ul_config_parse_file("/usr/share/unl0kr/unl0kr.conf", &conf_opts);
    ul_config_parse_directory("/usr/share/unl0kr/unl0kr.conf.d", &conf_opts);
    ul_config_parse_file("/etc/unl0kr.conf", &conf_opts);
    ul_config_parse_directory("/etc/unl0kr.conf.d", &conf_opts);
    ul_config_parse_files(cli_opts.config_files, cli_opts.num_config_files, &conf_opts);

    bbx_terminal_prepare_current_terminal(!conf_opts.quirks.terminal_prevent_graphics_mode,
        !conf_opts.quirks.terminal_allow_keyboard_input);
    struct sigaction action;
    memset(&action, 0, sizeof(action));
    action.sa_handler = exit_success;
    sigaction(SIGINT, &action, NULL);
    sigaction(SIGTERM, &action, NULL);
    if (cli_opts.logo && cli_opts.handover && cli_opts.backlight) {
        backlight_fd = open(cli_opts.backlight, O_RDWR | O_CLOEXEC);
        if (backlight_fd < 0)
            fprintf(stderr, "plinius-progress: cannot open %s: %s\n", cli_opts.backlight, strerror(errno));
    }
    if (cli_opts.logo && cli_opts.handover) {
        struct sigaction usr1;
        memset(&usr1, 0, sizeof(usr1));
        usr1.sa_handler = request_handover;
        sigaction(SIGUSR1, &usr1, NULL);
    }

    lv_init();
    lv_log_register_print_cb(bbx_log_print_cb);

    bbx_display_config_t display_config = {
        .hor_res = cli_opts.common.hor_res,
        .ver_res = cli_opts.common.ver_res,
        .x_offset = cli_opts.common.x_offset,
        .y_offset = cli_opts.common.y_offset,
        .dpi = cli_opts.common.dpi,
        .fbdev_force_refresh = conf_opts.quirks.fbdev_force_refresh
    };
    lv_display_t *disp = bbx_display_create(conf_opts.general.backend, &display_config);
    if (!disp)
        exit_failure();

    int fd_epoll = epoll_create1(EPOLL_CLOEXEC);
    if (fd_epoll == -1) {
        bbx_log(BBX_LOG_LEVEL_ERROR, "epoll_create1() failed");
        exit_failure();
    }

    /* Input only for the button. Deliberately no power key handler: pressing it during
     * the encryption must not shut the device down. */
    if (cli_opts.button) {
        struct bbx_indev_opts input_config = {
            .keymap = &conf_opts.hw_keyboard,
            .keyboard = conf_opts.input.keyboard,
            .pointer = conf_opts.input.pointer,
            .touchscreen = conf_opts.input.touchscreen,
            .force_feedback = conf_opts.keyboard.haptic_feedback
        };
        if (bbx_indev_init(fd_epoll, &input_config) == 0)
            exit_failure();
    }

    if (cli_opts.progress) {
        fcntl(STDIN_FILENO, F_SETFL, fcntl(STDIN_FILENO, F_GETFL) | O_NONBLOCK);
        struct epoll_event event = { .events = EPOLLIN };
        event.data.ptr = __extension__ (void *) on_stdin;
        if (epoll_ctl(fd_epoll, EPOLL_CTL_ADD, STDIN_FILENO, &event) == -1) {
            bbx_log(BBX_LOG_LEVEL_ERROR, "Cannot watch stdin");
            exit_failure();
        }
    }

    /* Same theme and accent color as the password prompt of unl0kr */
    const bbx_theme *theme = bbx_themes_get_with_accent(conf_opts.theme.default_id,
        conf_opts.theme.has_accent ? &conf_opts.theme.accent : NULL);
    bbx_theme_apply(theme);
    if (cli_opts.logo)
        build_logo(theme->bar.indicator.bg_color);
    else
        build_ui(disp, theme);

    if ((cli_opts.button || cli_opts.logo) && cli_opts.timeout > 0)
        lv_timer_create(timeout_cb, (uint32_t)cli_opts.timeout * 1000, NULL);

    while (1) {
        if (handover_requested)
            hand_over();
        uint32_t time_till_next = lv_timer_handler();

        struct epoll_event event;
        int r = epoll_wait(fd_epoll, &event, 1, time_till_next);
        if (r > 0) {
            __extension__ void (*handler)() = event.data.ptr;
            handler();
            continue;
        }
        if (r == 0 || errno == EINTR)
            continue;

        bbx_log(BBX_LOG_LEVEL_ERROR, "epoll_wait() failed");
        exit_failure();
    }
}
