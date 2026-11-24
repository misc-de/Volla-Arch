/* Stand-ins for LVGL and BuffyBox's shared code, as far as plinius-progress uses them
 * (tests/daemons-hw/test_dhw_progress.py). The real ones need the BuffyBox source tree, a display
 * and input devices. The objects record what is set on them (fake_progress.c) and are written
 * out as JSON when the program exits, so a test can check the view that was built. Constants
 * and the image header have LVGL 9's values and layout (lvgl 85aa60d1). */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* ---- LVGL ---- */

#define LV_USE_LINUX_DRM 1
#define LV_UNUSED(x) ((void)(x))
#define LV_ABS(x) ((x) > 0 ? (x) : (-(x)))
#define LV_MAX(a, b) ((a) > (b) ? (a) : (b))
#define LV_TRIGO_SIN_MAX 32768
#define LV_SCALE_NONE 256
/* A percentage as LVGL codes it: a flag bit above the value */
#define LV_PCT_FLAG (1 << 29)
#define LV_PCT(x) (LV_PCT_FLAG | (x))

typedef uint8_t lv_opa_t;
enum { LV_OPA_TRANSP = 0, LV_OPA_60 = 153, LV_OPA_80 = 204, LV_OPA_COVER = 255 };

typedef struct {
    uint32_t hex;
} lv_color_t;

typedef struct lv_obj_t lv_obj_t;
typedef struct lv_group_t lv_group_t;
typedef struct lv_display_t lv_display_t;
typedef struct lv_timer_t lv_timer_t;
typedef struct lv_event_t lv_event_t;
typedef void (*lv_event_cb_t)(lv_event_t *event);
typedef void (*lv_timer_cb_t)(lv_timer_t *timer);
typedef void (*lv_anim_exec_xcb_t)(void *var, int32_t value);
typedef int lv_log_level_t;
typedef void (*lv_log_print_g_cb_t)(lv_log_level_t level, const char *msg);

/* Selectors: parts and states */
#define LV_PART_MAIN 0x000000
#define LV_PART_INDICATOR 0x020000
#define LV_STATE_FOCUS_KEY 0x0004

enum { LV_EVENT_CLICKED = 10 };
enum { LV_ANIM_OFF = 0, LV_ANIM_ON = 1 };
#define LV_ANIM_REPEAT_INFINITE 0xFFFFFFFF
enum { LV_LABEL_LONG_MODE_WRAP = 0 };
enum { LV_TEXT_ALIGN_CENTER = 2 };
enum { LV_FLEX_FLOW_COLUMN = 1 };
enum { LV_FLEX_ALIGN_CENTER = 2 };
enum { LV_ALIGN_OUT_BOTTOM_MID = 17 };
enum { LV_OBJ_FLAG_SCROLLABLE = 1 << 4, LV_OBJ_FLAG_FLOATING = 1 << 17 };

#define LV_SYMBOL_OK "\xEF\x80\x8C"
#define LV_SYMBOL_WARNING "\xEF\x81\xB1"

/* lv_image_dsc.h */
#define LV_IMAGE_HEADER_MAGIC (0x19)
#define LV_COLOR_FORMAT_ARGB8888 0x10
typedef struct {
    uint32_t magic: 8;
    uint32_t cf : 8;
    uint32_t flags: 16;
    uint32_t w: 16;
    uint32_t h: 16;
    uint32_t stride: 16;
    uint32_t reserved_2: 16;
} lv_image_header_t;

typedef struct {
    lv_image_header_t header;
    uint32_t data_size;
    const uint8_t *data;
    const void *reserved;
    const void *reserved_2;
} lv_image_dsc_t;

typedef struct {
    void *var;
    lv_anim_exec_xcb_t exec_cb;
    int32_t start_value, end_value;
    uint32_t duration;
    uint32_t repeat_cnt;
} lv_anim_t;

void lv_init(void);
void lv_log_register_print_cb(lv_log_print_g_cb_t cb);
lv_obj_t *lv_screen_active(void);
void lv_theme_apply(lv_obj_t *obj);
void lv_refr_now(lv_display_t *disp);
uint32_t lv_timer_handler(void);
lv_timer_t *lv_timer_create(lv_timer_cb_t cb, uint32_t period, void *user_data);
int32_t lv_display_get_horizontal_resolution(const lv_display_t *disp);
int32_t lv_display_get_vertical_resolution(const lv_display_t *disp);
int32_t lv_dpx(int32_t n);
lv_color_t lv_color_hex(uint32_t c);
lv_color_t lv_color_black(void);
lv_color_t lv_color_white(void);
int32_t lv_trigo_sin(int16_t angle);
int32_t lv_trigo_cos(int16_t angle);

lv_obj_t *lv_label_create(lv_obj_t *parent);
lv_obj_t *lv_button_create(lv_obj_t *parent);
lv_obj_t *lv_bar_create(lv_obj_t *parent);
lv_obj_t *lv_image_create(lv_obj_t *parent);
void lv_label_set_text(lv_obj_t *obj, const char *text);
void lv_label_set_long_mode(lv_obj_t *obj, int mode);
void lv_bar_set_range(lv_obj_t *obj, int32_t min, int32_t max);
void lv_bar_set_value(lv_obj_t *obj, int32_t value, int anim);
void lv_image_set_src(lv_obj_t *obj, const void *src);
void lv_image_set_scale_x(lv_obj_t *obj, uint32_t scale);
void lv_obj_set_width(lv_obj_t *obj, int32_t w);
void lv_obj_set_size(lv_obj_t *obj, int32_t w, int32_t h);
void lv_obj_center(lv_obj_t *obj);
void lv_obj_add_flag(lv_obj_t *obj, uint32_t flag);
void lv_obj_remove_flag(lv_obj_t *obj, uint32_t flag);
void lv_obj_set_flex_flow(lv_obj_t *obj, int flow);
void lv_obj_set_flex_align(lv_obj_t *obj, int main, int cross, int track);
void lv_obj_update_layout(const lv_obj_t *obj);
void lv_obj_align_to(lv_obj_t *obj, const lv_obj_t *base, int align, int32_t x, int32_t y);
void lv_obj_add_event_cb(lv_obj_t *obj, lv_event_cb_t cb, int filter, void *user_data);
void lv_group_add_obj(lv_group_t *group, lv_obj_t *obj);
void lv_group_focus_obj(lv_obj_t *obj);

/* The style setters all record "name" with a number */
#define FAKE_STYLE(name, type) void lv_obj_set_style_##name(lv_obj_t *obj, type value, uint32_t selector);
FAKE_STYLE(text_align, int)
FAKE_STYLE(pad_hor, int32_t)
FAKE_STYLE(pad_ver, int32_t)
FAKE_STYLE(pad_row, int32_t)
FAKE_STYLE(pad_all, int32_t)
FAKE_STYLE(pad_left, int32_t)
FAKE_STYLE(pad_right, int32_t)
FAKE_STYLE(outline_width, int32_t)
FAKE_STYLE(outline_pad, int32_t)
FAKE_STYLE(outline_color, lv_color_t)
FAKE_STYLE(image_opa, lv_opa_t)
FAKE_STYLE(bg_color, lv_color_t)
FAKE_STYLE(bg_opa, lv_opa_t)
FAKE_STYLE(text_color, lv_color_t)
FAKE_STYLE(text_opa, lv_opa_t)
FAKE_STYLE(border_width, int32_t)
FAKE_STYLE(border_color, lv_color_t)
FAKE_STYLE(radius, int32_t)
FAKE_STYLE(anim_duration, uint32_t)
#undef FAKE_STYLE

void lv_anim_init(lv_anim_t *a);
void lv_anim_set_var(lv_anim_t *a, void *var);
void lv_anim_set_exec_cb(lv_anim_t *a, lv_anim_exec_xcb_t cb);
void lv_anim_set_values(lv_anim_t *a, int32_t start, int32_t end);
void lv_anim_set_duration(lv_anim_t *a, uint32_t duration);
void lv_anim_set_repeat_count(lv_anim_t *a, uint32_t count);
void lv_anim_start(const lv_anim_t *a);
bool lv_anim_delete(void *var, lv_anim_exec_xcb_t cb);

/* ---- BuffyBox shared code ---- */

typedef enum { BBX_LOG_LEVEL_ERROR = 0, BBX_LOG_LEVEL_WARNING = 1, BBX_LOG_LEVEL_VERBOSE = 2 } bbx_log_level;
void bbx_log_set_level(bbx_log_level level);
void bbx_log(bbx_log_level level, const char *format, ...) __attribute__((format(printf, 2, 3)));
void bbx_log_print_cb(lv_log_level_t level, const char *msg);

typedef struct {
    int hor_res, ver_res, x_offset, y_offset, dpi;
    bool verbose;
} bbx_cli_common_opts;
void bbx_cli_init_common_opts(bbx_cli_common_opts *opts);
int bbx_cli_parse_geometry(const char *optarg, bbx_cli_common_opts *opts);
int bbx_cli_parse_dpi(const char *optarg, bbx_cli_common_opts *opts);
void bbx_cli_print_version_and_exit(const char *program_name);

typedef int bbx_backends_backend_id_t;
typedef struct {
    int hor_res, ver_res, x_offset, y_offset, dpi;
    bool fbdev_force_refresh;
} bbx_display_config_t;
lv_display_t *bbx_display_create(bbx_backends_backend_id_t backend_id, bbx_display_config_t *config);

struct xkb_rule_names {
    const char *rules, *model, *layout, *variant, *options;
};
struct bbx_indev_opts {
    struct xkb_rule_names *keymap;
    uint8_t keyboard : 1;
    uint8_t pointer : 1;
    uint8_t touchscreen : 1;
    uint8_t force_feedback : 1;
};
extern lv_group_t *keyboard_input_group;
uint8_t bbx_indev_init(int fd_epoll, const struct bbx_indev_opts *opts);

void bbx_terminal_prepare_current_terminal(bool enable_graphics_mode, bool disable_keyboard_input);
void bbx_terminal_reset_current_terminal(void);

void bbx_background_apply(lv_obj_t *screen, const char *path);

typedef int bbx_themes_theme_id_t;
typedef struct {
    struct {
        int32_t border_width;
        uint32_t border_color;
        int32_t corner_radius;
        struct {
            uint32_t bg_color;
        } indicator;
    } bar;
    struct {
        bool enabled;
        lv_opa_t surface_opa;
    } glass;
} bbx_theme;
const bbx_theme *bbx_themes_get_with_accent(bbx_themes_theme_id_t id, const uint32_t *accent);
void bbx_theme_apply(const bbx_theme *theme);

/* ---- unl0kr's configuration (with the background and accent of our patches) ---- */

typedef struct {
    struct {
        bool animations;
        bbx_backends_backend_id_t backend;
    } general;
    struct {
        bool haptic_feedback;
    } keyboard;
    struct {
        bbx_themes_theme_id_t default_id;
        const char *background;
        bool has_accent;
        uint32_t accent;
    } theme;
    struct {
        bool keyboard, pointer, touchscreen;
    } input;
    struct xkb_rule_names hw_keyboard;
    struct {
        bool fbdev_force_refresh;
        bool terminal_prevent_graphics_mode;
        bool terminal_allow_keyboard_input;
    } quirks;
} ul_config_opts;
void ul_config_init_opts(ul_config_opts *opts);
void ul_config_parse_file(const char *path, ul_config_opts *opts);
void ul_config_parse_directory(const char *path, ul_config_opts *opts);
void ul_config_parse_files(const char **files, int num_files, ul_config_opts *opts);
