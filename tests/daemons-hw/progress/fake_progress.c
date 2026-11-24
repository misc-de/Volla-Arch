/* The stand-ins of fake_progress.h, plus the system calls plinius-progress makes on the phone's
 * display: DRM (fstat finding a card, drmDropMaster, the CRTCs scanning out the logo), the
 * sleeps of the handover (a fake clock, so 30 s pass at once), pwrite on the backlight. Included
 * by the harness after the program, with the program's calls of fstat, usleep, pwrite, read,
 * epoll_wait and _exit renamed to the fake_ ones below.
 *
 * Environment:
 *   FAKE_LOG          file the calls are logged to, one line each
 *   FAKE_TREE         file the objects are written to as JSON when the program exits
 *   FAKE_RES          display size WxH (1080x2400)
 *   FAKE_NO_DISPLAY   bbx_display_create fails
 *   FAKE_INDEV        what bbx_indev_init returns (1)
 *   FAKE_PRESS        n: the n-th button with a click handler is clicked in the second loop
 *   FAKE_ANIM_ANGLES  "a,b,...": the logo's animation is run at these angles in the first loop
 *   FAKE_DRM_FD       this fd is a DRM card (226:0), FAKE_RENDER_FD a render node (226:128)
 *   FAKE_DROP_MASTER_ERRNO  drmDropMaster fails with this errno
 *   FAKE_CRTCS        "id:fb,..." the CRTCs and what they scan out; ":gone" vanishes once
 *                     replaced, "id:missing" cannot be read at all; FAKE_NO_RESOURCES: none
 *   FAKE_REPLACED_AT_MS  from this time of the fake clock on the CRTCs show the compositor's fb
 *   FAKE_READ_EAGAIN, FAKE_EPOLL_FAIL, FAKE_PWRITE_FAIL  the first read says EAGAIN, epoll_wait
 *                     fails, pwrite fails */
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <time.h>
#include <unistd.h>
#include <xf86drm.h>
#include <xf86drmMode.h>

static void flog(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void flog(const char *fmt, ...)
{
    static int fd = -2;
    if (fd == -2) {
        const char *path = getenv("FAKE_LOG");
        fd = path ? open(path, O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC, 0644) : -1;
    }
    if (fd < 0)
        return;
    char line[2048];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(line, sizeof(line) - 1, fmt, ap);
    va_end(ap);
    if (n < 0)
        return;
    if (n > (int)sizeof(line) - 2)
        n = sizeof(line) - 2;
    line[n++] = '\n';
    (void)!write(fd, line, n);
}

static int env_int(const char *name, int fallback)
{
    const char *v = getenv(name);
    return v && *v ? atoi(v) : fallback;
}

/* ---- Objects ---- */

struct style {
    char name[24];
    uint32_t selector;
    long value;
    bool color;
};

struct lv_obj_t {
    int id;
    const char *type;
    lv_obj_t *parent;
    char *text;
    uint32_t flags;
    int32_t width, height;
    bool has_width, has_height, centered, focused, in_group, layout_updated;
    int long_mode;
    int32_t bar_min, bar_max, bar_value;
    bool has_range;
    lv_event_cb_t click_cb;
    const lv_image_dsc_t *src;
    uint32_t scale_x;
    const lv_obj_t *align_base;
    int align, align_x, align_y;
    bool aligned;
    struct style styles[48];
    int n_styles;
};

struct lv_group_t {
    int unused;
};
struct lv_display_t {
    int32_t hor, ver;
};
struct lv_timer_t {
    lv_timer_cb_t cb;
    uint32_t period;
    int64_t start;
};
struct lv_event_t {
    int code;
};

static lv_obj_t *objects[64];
static int n_objects;
static lv_obj_t *screen;
static struct lv_group_t the_group;
lv_group_t *keyboard_input_group = &the_group;
static struct lv_timer_t timers[8];
static int n_timers;
static lv_anim_t anim;
static bool anim_running;

static lv_obj_t *new_obj(const char *type, lv_obj_t *parent)
{
    if (n_objects == 64)
        abort();
    lv_obj_t *o = calloc(1, sizeof(*o));
    o->id = n_objects;
    o->type = type;
    o->parent = parent;
    o->scale_x = LV_SCALE_NONE;
    objects[n_objects++] = o;
    return o;
}

static void set_style(lv_obj_t *o, const char *name, long value, uint32_t selector, bool color)
{
    for (int i = 0; i < o->n_styles; i++)
        if (!strcmp(o->styles[i].name, name) && o->styles[i].selector == selector) {
            o->styles[i].value = value;
            return;
        }
    if (o->n_styles == 48)
        abort();
    struct style *s = &o->styles[o->n_styles++];
    snprintf(s->name, sizeof(s->name), "%s", name);
    s->selector = selector;
    s->value = value;
    s->color = color;
}

static long style_of(const lv_obj_t *o, const char *name, uint32_t selector, long fallback)
{
    for (int i = 0; i < o->n_styles; i++)
        if (!strcmp(o->styles[i].name, name) && o->styles[i].selector == selector)
            return o->styles[i].value;
    return fallback;
}

static const char *selector_name(uint32_t s)
{
    switch (s) {
    case LV_PART_MAIN: return "main";
    case LV_PART_INDICATOR: return "indicator";
    case LV_STATE_FOCUS_KEY: return "focus_key";
    default: return "other";
    }
}

/* A size as text: "100%" for LV_PCT, else the number */
static void put_size(FILE *f, const char *key, int32_t v)
{
    if (v & LV_PCT_FLAG)
        fprintf(f, ",\"%s\":\"%d%%\"", key, v & ~LV_PCT_FLAG);
    else
        fprintf(f, ",\"%s\":%d", key, v);
}

static void put_string(FILE *f, const char *s)
{
    fputc('"', f);
    for (const unsigned char *p = (const unsigned char *)s; *p; p++) {
        if (*p == '"' || *p == '\\')
            fprintf(f, "\\%c", *p);
        else if (*p < 0x20)
            fprintf(f, "\\u%04x", *p);
        else
            fputc(*p, f);
    }
    fputc('"', f);
}

/* All objects as a JSON array; image data next to it as <FAKE_TREE>.<id>.bin */
static void dump_tree(void)
{
    const char *path = getenv("FAKE_TREE");
    if (!path)
        return;
    FILE *f = fopen(path, "w");
    if (!f)
        return;
    fputc('[', f);
    for (int i = 0; i < n_objects; i++) {
        lv_obj_t *o = objects[i];
        fprintf(f, "%s{\"id\":%d,\"type\":\"%s\",\"parent\":%d", i ? ",\n" : "", o->id, o->type,
                o->parent ? o->parent->id : -1);
        if (o->text) {
            fputs(",\"text\":", f);
            put_string(f, o->text);
        }
        fprintf(f, ",\"flags\":%u,\"centered\":%s,\"focused\":%s,\"in_group\":%s,\"clickable\":%s,"
                   "\"layout_updated\":%s,\"scale_x\":%u",
                o->flags, o->centered ? "true" : "false", o->focused ? "true" : "false",
                o->in_group ? "true" : "false", o->click_cb ? "true" : "false",
                o->layout_updated ? "true" : "false", o->scale_x);
        if (o->has_width)
            put_size(f, "width", o->width);
        if (o->has_height)
            put_size(f, "height", o->height);
        if (o->has_range)
            fprintf(f, ",\"range\":[%d,%d],\"value\":%d", o->bar_min, o->bar_max, o->bar_value);
        if (o->aligned)
            fprintf(f, ",\"align_to\":%d,\"align\":%d,\"align_x\":%d,\"align_y\":%d", o->align_base->id, o->align,
                    o->align_x, o->align_y);
        if (o->src) {
            char img[4096];
            snprintf(img, sizeof(img), "%s.%d.bin", path, o->id);
            FILE *d = fopen(img, "wb");
            if (d) {
                fwrite(o->src->data, 1, o->src->data_size, d);
                fclose(d);
            }
            fprintf(f, ",\"image\":{\"w\":%u,\"h\":%u,\"stride\":%u,\"cf\":%u,\"size\":%u}", o->src->header.w,
                    o->src->header.h, o->src->header.stride, o->src->header.cf, o->src->data_size);
        }
        fputs(",\"styles\":{", f);
        for (int s = 0; s < o->n_styles; s++) {
            const struct style *st = &o->styles[s];
            if (st->color)
                fprintf(f, "%s\"%s/%s\":\"#%06lx\"", s ? "," : "", st->name, selector_name(st->selector), st->value);
            else
                fprintf(f, "%s\"%s/%s\":%ld", s ? "," : "", st->name, selector_name(st->selector), st->value);
        }
        fputs("}}", f);
    }
    fputs("]\n", f);
    fclose(f);
}

__attribute__((constructor)) static void fake_setup(void)
{
    atexit(dump_tree);
}

/* ---- LVGL ---- */

void lv_init(void)
{
    flog("LV_INIT");
}

void lv_log_register_print_cb(lv_log_print_g_cb_t cb)
{
    flog("LOG_CB %s", cb == bbx_log_print_cb ? "bbx" : "other");
}

lv_obj_t *lv_screen_active(void)
{
    if (!screen)
        screen = new_obj("screen", NULL);
    return screen;
}

void lv_theme_apply(lv_obj_t *obj)
{
    flog("THEME_APPLY_OBJ %d", obj->id);
}

void lv_refr_now(lv_display_t *disp)
{
    lv_obj_t *mark = NULL;
    for (int i = 0; i < n_objects; i++)
        if (objects[i]->src && !mark)
            mark = objects[i];
    /* What the frame shows of the mark: its turn and opacity */
    if (mark)
        flog("REFRESH mark scale_x=%u opa=%ld", mark->scale_x, style_of(mark, "image_opa", LV_PART_MAIN, 255));
    else
        flog("REFRESH");
}

static int64_t mono_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000LL + ts.tv_nsec / 1000000;
}

lv_timer_t *lv_timer_create(lv_timer_cb_t cb, uint32_t period, void *user_data)
{
    if (n_timers == 8)
        abort();
    lv_timer_t *t = &timers[n_timers++];
    t->cb = cb;
    t->period = period;
    t->start = mono_ms();
    flog("TIMER %u", period);
    return t;
}

uint32_t lv_timer_handler(void)
{
    static int loops;
    loops++;
    if (loops == 1) {
        flog("LOOP");
        const char *angles = getenv("FAKE_ANIM_ANGLES");
        if (angles && anim_running) {
            char *copy = strdup(angles), *save = NULL;
            for (char *a = strtok_r(copy, ",", &save); a; a = strtok_r(NULL, ",", &save)) {
                lv_obj_t *mark = anim.var;
                anim.exec_cb(anim.var, atoi(a));
                flog("TURN %d scale_x=%u opa=%ld", atoi(a), mark->scale_x,
                     style_of(mark, "image_opa", LV_PART_MAIN, 255));
            }
            free(copy);
        }
        /* The test waits for this before it sends a signal */
        (void)!write(2, "LOOP\n", 5);
    }
    if (loops == 2 && getenv("FAKE_PRESS")) {
        int want = atoi(getenv("FAKE_PRESS")), seen = 0;
        for (int i = 0; i < n_objects; i++)
            if (objects[i]->click_cb && ++seen == want) {
                struct lv_event_t ev = {LV_EVENT_CLICKED};
                flog("PRESS %d", objects[i]->id);
                objects[i]->click_cb(&ev);
            }
    }
    int64_t now = mono_ms();
    for (int i = 0; i < n_timers; i++)
        if (now - timers[i].start >= timers[i].period) {
            timers[i].start = now;
            flog("TIMER_FIRED %u", timers[i].period);
            timers[i].cb(&timers[i]);
        }
    return 5;
}

int32_t lv_display_get_horizontal_resolution(const lv_display_t *disp)
{
    return disp->hor;
}

int32_t lv_display_get_vertical_resolution(const lv_display_t *disp)
{
    return disp->ver;
}

/* As at 320 dpi */
int32_t lv_dpx(int32_t n)
{
    return n * 2;
}

lv_color_t lv_color_hex(uint32_t c)
{
    return (lv_color_t){c & 0xffffff};
}

lv_color_t lv_color_black(void)
{
    return (lv_color_t){0};
}

lv_color_t lv_color_white(void)
{
    return (lv_color_t){0xffffff};
}

/* LVGL's table: round(32767 sin a) per degree, with ±32767 widened to ±32768 */
int32_t lv_trigo_sin(int16_t angle)
{
    while (angle < 0)
        angle += 360;
    while (angle >= 360)
        angle -= 360;
    static const uint16_t table[91] = {
        0,     572,   1144,  1715,  2286,  2856,  3425,  3993,  4560,  5126,  5690,  6252,  6813,  7371,
        7927,  8481,  9032,  9580,  10126, 10668, 11207, 11743, 12275, 12803, 13328, 13848, 14365, 14876,
        15384, 15886, 16384, 16877, 17364, 17847, 18324, 18795, 19261, 19720, 20174, 20622, 21063, 21498,
        21926, 22348, 22763, 23170, 23571, 23965, 24351, 24730, 25102, 25466, 25822, 26170, 26510, 26842,
        27166, 27482, 27789, 28088, 28378, 28660, 28932, 29197, 29452, 29698, 29935, 30163, 30382, 30592,
        30792, 30983, 31164, 31336, 31499, 31651, 31795, 31928, 32052, 32166, 32270, 32365, 32449, 32524,
        32588, 32643, 32688, 32723, 32748, 32763, 32767};
    int32_t ret;
    if (angle < 90)
        ret = table[angle];
    else if (angle < 180)
        ret = table[180 - angle];
    else if (angle < 270)
        ret = -table[angle - 180];
    else
        ret = -table[360 - angle];
    return ret == 32767 ? 32768 : ret == -32767 ? -32768 : ret;
}

int32_t lv_trigo_cos(int16_t angle)
{
    return lv_trigo_sin(angle + 90);
}

lv_obj_t *lv_label_create(lv_obj_t *parent)
{
    return new_obj("label", parent);
}

lv_obj_t *lv_button_create(lv_obj_t *parent)
{
    return new_obj("button", parent);
}

lv_obj_t *lv_bar_create(lv_obj_t *parent)
{
    return new_obj("bar", parent);
}

lv_obj_t *lv_image_create(lv_obj_t *parent)
{
    return new_obj("image", parent);
}

void lv_label_set_text(lv_obj_t *obj, const char *text)
{
    free(obj->text);
    obj->text = strdup(text);
    flog("TEXT %d %s", obj->id, text);
}

void lv_label_set_long_mode(lv_obj_t *obj, int mode)
{
    obj->long_mode = mode;
    set_style(obj, "long_mode", mode, LV_PART_MAIN, false);
}

void lv_bar_set_range(lv_obj_t *obj, int32_t min, int32_t max)
{
    obj->has_range = true;
    obj->bar_min = min;
    obj->bar_max = max;
}

void lv_bar_set_value(lv_obj_t *obj, int32_t value, int anim_on)
{
    obj->bar_value = value;
    flog("BAR %d %s", value, anim_on == LV_ANIM_ON ? "anim" : "no-anim");
}

void lv_image_set_src(lv_obj_t *obj, const void *src)
{
    obj->src = src;
}

void lv_image_set_scale_x(lv_obj_t *obj, uint32_t scale)
{
    obj->scale_x = scale;
}

void lv_obj_set_width(lv_obj_t *obj, int32_t w)
{
    obj->has_width = true;
    obj->width = w;
}

void lv_obj_set_size(lv_obj_t *obj, int32_t w, int32_t h)
{
    obj->has_width = obj->has_height = true;
    obj->width = w;
    obj->height = h;
}

void lv_obj_center(lv_obj_t *obj)
{
    obj->centered = true;
}

void lv_obj_add_flag(lv_obj_t *obj, uint32_t flag)
{
    obj->flags |= flag;
}

void lv_obj_remove_flag(lv_obj_t *obj, uint32_t flag)
{
    /* Recorded as removed: a negative entry under styles */
    obj->flags &= ~flag;
    set_style(obj, "removed_flags", (long)style_of(obj, "removed_flags", LV_PART_MAIN, 0) | flag, LV_PART_MAIN,
              false);
}

void lv_obj_set_flex_flow(lv_obj_t *obj, int flow)
{
    set_style(obj, "flex_flow", flow, LV_PART_MAIN, false);
}

void lv_obj_set_flex_align(lv_obj_t *obj, int main, int cross, int track)
{
    set_style(obj, "flex_align", main * 100 + cross * 10 + track, LV_PART_MAIN, false);
}

void lv_obj_update_layout(const lv_obj_t *obj)
{
    ((lv_obj_t *)obj)->layout_updated = true;
}

void lv_obj_align_to(lv_obj_t *obj, const lv_obj_t *base, int align, int32_t x, int32_t y)
{
    obj->aligned = true;
    obj->align_base = base;
    obj->align = align;
    obj->align_x = x;
    obj->align_y = y;
}

void lv_obj_add_event_cb(lv_obj_t *obj, lv_event_cb_t cb, int filter, void *user_data)
{
    if (filter == LV_EVENT_CLICKED)
        obj->click_cb = cb;
}

void lv_group_add_obj(lv_group_t *group, lv_obj_t *obj)
{
    obj->in_group = group == keyboard_input_group;
}

void lv_group_focus_obj(lv_obj_t *obj)
{
    obj->focused = true;
}

#define FAKE_STYLE_NUM(name, type)                                                    \
    void lv_obj_set_style_##name(lv_obj_t *obj, type value, uint32_t selector)        \
    {                                                                                 \
        set_style(obj, #name, (long)value, selector, false);                          \
    }
#define FAKE_STYLE_COLOR(name)                                                        \
    void lv_obj_set_style_##name(lv_obj_t *obj, lv_color_t value, uint32_t selector)  \
    {                                                                                 \
        set_style(obj, #name, (long)value.hex, selector, true);                       \
    }
FAKE_STYLE_NUM(text_align, int)
FAKE_STYLE_NUM(pad_hor, int32_t)
FAKE_STYLE_NUM(pad_ver, int32_t)
FAKE_STYLE_NUM(pad_row, int32_t)
FAKE_STYLE_NUM(pad_all, int32_t)
FAKE_STYLE_NUM(pad_left, int32_t)
FAKE_STYLE_NUM(pad_right, int32_t)
FAKE_STYLE_NUM(outline_width, int32_t)
FAKE_STYLE_NUM(outline_pad, int32_t)
FAKE_STYLE_COLOR(outline_color)
FAKE_STYLE_NUM(image_opa, lv_opa_t)
FAKE_STYLE_COLOR(bg_color)
FAKE_STYLE_NUM(bg_opa, lv_opa_t)
FAKE_STYLE_COLOR(text_color)
FAKE_STYLE_NUM(text_opa, lv_opa_t)
FAKE_STYLE_NUM(border_width, int32_t)
FAKE_STYLE_COLOR(border_color)
FAKE_STYLE_NUM(radius, int32_t)
FAKE_STYLE_NUM(anim_duration, uint32_t)

void lv_anim_init(lv_anim_t *a)
{
    memset(a, 0, sizeof(*a));
}

void lv_anim_set_var(lv_anim_t *a, void *var)
{
    a->var = var;
}

void lv_anim_set_exec_cb(lv_anim_t *a, lv_anim_exec_xcb_t cb)
{
    a->exec_cb = cb;
}

void lv_anim_set_values(lv_anim_t *a, int32_t start, int32_t end)
{
    a->start_value = start;
    a->end_value = end;
}

void lv_anim_set_duration(lv_anim_t *a, uint32_t duration)
{
    a->duration = duration;
}

void lv_anim_set_repeat_count(lv_anim_t *a, uint32_t count)
{
    a->repeat_cnt = count;
}

void lv_anim_start(const lv_anim_t *a)
{
    anim = *a;
    anim_running = true;
    flog("ANIM obj=%d values=%d..%d duration=%u repeat=%s", ((lv_obj_t *)a->var)->id, a->start_value,
         a->end_value, a->duration, a->repeat_cnt == LV_ANIM_REPEAT_INFINITE ? "infinite" : "finite");
}

bool lv_anim_delete(void *var, lv_anim_exec_xcb_t cb)
{
    bool found = anim_running && anim.var == var && (!cb || cb == anim.exec_cb);
    if (found)
        anim_running = false;
    flog("ANIM_DELETE obj=%d %s", ((lv_obj_t *)var)->id, found ? "stopped" : "none");
    return found;
}

/* ---- BuffyBox ---- */

void bbx_log_set_level(bbx_log_level level)
{
    flog("LOG_LEVEL %d", level);
}

void bbx_log(bbx_log_level level, const char *format, ...)
{
    char msg[1024];
    va_list ap;
    va_start(ap, format);
    vsnprintf(msg, sizeof(msg), format, ap);
    va_end(ap);
    flog("BBX_LOG %d %s", level, msg);
    fprintf(stderr, "%s\n", msg);
}

void bbx_log_print_cb(lv_log_level_t level, const char *msg)
{
}

void bbx_cli_init_common_opts(bbx_cli_common_opts *opts)
{
    *opts = (bbx_cli_common_opts){.hor_res = -1, .ver_res = -1, .x_offset = 0, .y_offset = 0, .dpi = 0};
}

int bbx_cli_parse_geometry(const char *arg, bbx_cli_common_opts *opts)
{
    int n = sscanf(arg, "%dx%d@%d,%d", &opts->hor_res, &opts->ver_res, &opts->x_offset, &opts->y_offset);
    if (n != 2 && n != 4) {
        fprintf(stderr, "Invalid geometry %s\n", arg);
        return -1;
    }
    return 0;
}

int bbx_cli_parse_dpi(const char *arg, bbx_cli_common_opts *opts)
{
    opts->dpi = atoi(arg);
    if (opts->dpi <= 0) {
        fprintf(stderr, "Invalid dpi %s\n", arg);
        return -1;
    }
    return 0;
}

void bbx_cli_print_version_and_exit(const char *program_name)
{
    printf("%s %s\n", program_name, "fake-version");
    exit(0);
}

lv_display_t *bbx_display_create(bbx_backends_backend_id_t backend_id, bbx_display_config_t *c)
{
    flog("DISPLAY backend=%d hor=%d ver=%d x=%d y=%d dpi=%d force_refresh=%d", backend_id, c->hor_res, c->ver_res,
         c->x_offset, c->y_offset, c->dpi, c->fbdev_force_refresh);
    if (getenv("FAKE_NO_DISPLAY"))
        return NULL;
    static struct lv_display_t disp;
    if (sscanf(getenv("FAKE_RES") ? getenv("FAKE_RES") : "1080x2400", "%dx%d", &disp.hor, &disp.ver) != 2)
        abort();
    return &disp;
}

uint8_t bbx_indev_init(int fd_epoll, const struct bbx_indev_opts *o)
{
    flog("INDEV keyboard=%d pointer=%d touchscreen=%d feedback=%d keymap=%s", o->keyboard, o->pointer,
         o->touchscreen, o->force_feedback, o->keymap ? o->keymap->layout : "none");
    return (uint8_t)env_int("FAKE_INDEV", 1);
}

void bbx_terminal_prepare_current_terminal(bool enable_graphics_mode, bool disable_keyboard_input)
{
    flog("TERMINAL_PREPARE graphics=%d no_keyboard=%d", enable_graphics_mode, disable_keyboard_input);
}

void bbx_terminal_reset_current_terminal(void)
{
    flog("TERMINAL_RESET");
    /* A test that took away the file descriptors (epoll_create1 failing) gives them back here,
     * so that gcov can still write its counters at the exit that follows */
    struct rlimit rl;
    if (getrlimit(RLIMIT_NOFILE, &rl) == 0 && rl.rlim_cur < rl.rlim_max) {
        rl.rlim_cur = rl.rlim_max;
        setrlimit(RLIMIT_NOFILE, &rl);
    }
}

void bbx_background_apply(lv_obj_t *obj, const char *path)
{
    flog("BACKGROUND %d %s", obj->id, path);
}

const bbx_theme *bbx_themes_get_with_accent(bbx_themes_theme_id_t id, const uint32_t *accent)
{
    static bbx_theme theme;
    if (accent)
        flog("THEME id=%d accent=%06x", id, *accent);
    else
        flog("THEME id=%d accent=none", id);
    theme.bar.border_width = 2;
    theme.bar.border_color = 0x123456;
    theme.bar.corner_radius = 5;
    theme.bar.indicator.bg_color = accent ? *accent : 0x3584e4;
    /* Theme 1 is the glass one */
    theme.glass.enabled = id == 1;
    theme.glass.surface_opa = 40;
    return &theme;
}

void bbx_theme_apply(const bbx_theme *theme)
{
    flog("THEME_APPLY");
}

/* ---- Configuration: only the files given with -C are read, as key=value lines ---- */

static bool truth(const char *v)
{
    return !strcmp(v, "true") || !strcmp(v, "1");
}

static void parse_one(const char *path, ul_config_opts *opts)
{
    FILE *f = fopen(path, "r");
    if (!f) {
        flog("CONFIG_MISSING %s", path);
        return;
    }
    char line[512];
    while (fgets(line, sizeof(line), f)) {
        line[strcspn(line, "\n")] = 0;
        char *v = strchr(line, '=');
        if (!v)
            continue;
        *v++ = 0;
        if (!strcmp(line, "animations"))
            opts->general.animations = truth(v);
        else if (!strcmp(line, "backend"))
            opts->general.backend = atoi(v);
        else if (!strcmp(line, "theme"))
            opts->theme.default_id = atoi(v);
        else if (!strcmp(line, "background"))
            opts->theme.background = strdup(v);
        else if (!strcmp(line, "accent")) {
            opts->theme.has_accent = true;
            opts->theme.accent = strtoul(v, NULL, 16);
        } else if (!strcmp(line, "keyboard"))
            opts->input.keyboard = truth(v);
        else if (!strcmp(line, "pointer"))
            opts->input.pointer = truth(v);
        else if (!strcmp(line, "touchscreen"))
            opts->input.touchscreen = truth(v);
        else if (!strcmp(line, "haptic"))
            opts->keyboard.haptic_feedback = truth(v);
        else if (!strcmp(line, "layout"))
            opts->hw_keyboard.layout = strdup(v);
        else if (!strcmp(line, "fbdev_force_refresh"))
            opts->quirks.fbdev_force_refresh = truth(v);
        else if (!strcmp(line, "prevent_graphics"))
            opts->quirks.terminal_prevent_graphics_mode = truth(v);
        else if (!strcmp(line, "allow_keyboard"))
            opts->quirks.terminal_allow_keyboard_input = truth(v);
    }
    fclose(f);
}

void ul_config_init_opts(ul_config_opts *opts)
{
    memset(opts, 0, sizeof(*opts));
    opts->input.keyboard = opts->input.pointer = opts->input.touchscreen = true;
    opts->hw_keyboard.layout = "us";
}

/* The system's files are only noted, never read: the host's would change the result */
void ul_config_parse_file(const char *path, ul_config_opts *opts)
{
    flog("CONFIG_FILE %s", path);
}

void ul_config_parse_directory(const char *path, ul_config_opts *opts)
{
    flog("CONFIG_DIR %s", path);
}

void ul_config_parse_files(const char **files, int num_files, ul_config_opts *opts)
{
    for (int i = 0; i < num_files; i++) {
        flog("CONFIG_OVERRIDE %s", files[i]);
        parse_one(files[i], opts);
    }
}

/* ---- The display's DRM device and the system calls of the handover ---- */

static int64_t fake_clock_ms;

int fake_fstat(int fd, struct stat *st)
{
    int r = fstatat(fd, "", st, AT_EMPTY_PATH);
    int minor_no = fd == env_int("FAKE_DRM_FD", -1) ? 0 : fd == env_int("FAKE_RENDER_FD", -1) ? 128 : -1;
    if (minor_no < 0)
        return r;
    memset(st, 0, sizeof(*st));
    st->st_mode = S_IFCHR | 0660;
    st->st_rdev = makedev(226, minor_no);
    return 0;
}

int fake_usleep(useconds_t us)
{
    fake_clock_ms += us / 1000;
    return 0;
}

ssize_t fake_pwrite(int fd, const void *buf, size_t n, off_t offset)
{
    if (getenv("FAKE_PWRITE_FAIL")) {
        errno = EIO;
        return -1;
    }
    flog("PWRITE %.*s@%ld", (int)n, (const char *)buf, (long)offset);
    return pwrite(fd, buf, n, offset);
}

ssize_t fake_read(int fd, void *buf, size_t n)
{
    static bool once;
    if (getenv("FAKE_READ_EAGAIN") && !once) {
        once = true;
        flog("READ EAGAIN");
        errno = EAGAIN;
        return -1;
    }
    return read(fd, buf, n);
}

int fake_epoll_wait(int epfd, struct epoll_event *events, int max, int timeout)
{
    if (getenv("FAKE_EPOLL_FAIL")) {
        errno = EBADF;
        return -1;
    }
    return epoll_wait(epfd, events, max, timeout);
}

void fake_exit_now(int status)
{
    flog("_EXIT %d", status);
    char comm[32] = "";
    FILE *f = fopen("/proc/self/comm", "r");
    if (f) {
        if (fgets(comm, sizeof(comm), f))
            comm[strcspn(comm, "\n")] = 0;
        fclose(f);
    }
    flog("COMM %s", comm);
    /* exit, not _exit: gcov's counters and the object dump are written */
    exit(status);
}

int drmDropMaster(int fd)
{
    flog("DROP_MASTER fd=%d", fd);
    int e = env_int("FAKE_DROP_MASTER_ERRNO", 0);
    if (e) {
        errno = e;
        return -1;
    }
    return 0;
}

struct fake_crtc {
    uint32_t id, fb;
    bool gone, missing;
};

static int fake_crtcs(struct fake_crtc *out, int max)
{
    const char *spec = getenv("FAKE_CRTCS");
    if (!spec)
        return 0;
    char *copy = strdup(spec), *save = NULL;
    int n = 0;
    for (char *c = strtok_r(copy, ",", &save); c && n < max; c = strtok_r(NULL, ",", &save)) {
        struct fake_crtc *f = &out[n++];
        memset(f, 0, sizeof(*f));
        f->id = strtoul(c, &c, 10);
        if (*c == ':')
            c++;
        if (!strncmp(c, "missing", 7))
            f->missing = true;
        else
            f->fb = strtoul(c, &c, 10);
        f->gone = strstr(c, ":gone") != NULL;
    }
    free(copy);
    return n;
}

drmModeResPtr drmModeGetResources(int fd)
{
    if (getenv("FAKE_NO_RESOURCES"))
        return NULL;
    struct fake_crtc c[16];
    int n = fake_crtcs(c, 16);
    drmModeResPtr res = calloc(1, sizeof(*res));
    res->count_crtcs = n;
    res->crtcs = calloc(n ? n : 1, sizeof(uint32_t));
    for (int i = 0; i < n; i++)
        res->crtcs[i] = c[i].id;
    return res;
}

void drmModeFreeResources(drmModeResPtr res)
{
    if (!res)
        return;
    free(res->crtcs);
    free(res);
}

drmModeCrtcPtr drmModeGetCrtc(int fd, uint32_t id)
{
    struct fake_crtc c[16];
    int n = fake_crtcs(c, 16);
    int replaced_at = env_int("FAKE_REPLACED_AT_MS", -1);
    bool replaced = replaced_at >= 0 && fake_clock_ms >= replaced_at;
    if (fd != env_int("FAKE_DRM_FD", -1))
        return NULL;
    for (int i = 0; i < n; i++) {
        if (c[i].id != id)
            continue;
        if (c[i].missing || (replaced && c[i].gone))
            return NULL;
        drmModeCrtcPtr crtc = calloc(1, sizeof(*crtc));
        crtc->crtc_id = id;
        /* The compositor's framebuffer once it took over */
        crtc->buffer_id = replaced && c[i].fb ? 99 : c[i].fb;
        return crtc;
    }
    return NULL;
}

void drmModeFreeCrtc(drmModeCrtcPtr crtc)
{
    free(crtc);
}
