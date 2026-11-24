/* What meson's gnome.mkenums_simple ('cc-number-row-enums', sources: cc-number-row.h) makes for
 * panels/common/cc-number-row.c, written by hand: the host has no glib-mkenums.
 * tests/gcc/test_gcc_display.py checks it against the enums of the tree's cc-number-row.h. */
#pragma once

#include <glib-object.h>

G_BEGIN_DECLS

GType cc_number_order_get_type (void);
#define CC_TYPE_NUMBER_ORDER (cc_number_order_get_type ())
GType cc_number_value_type_get_type (void);
#define CC_TYPE_NUMBER_VALUE_TYPE (cc_number_value_type_get_type ())
GType cc_number_sort_type_get_type (void);
#define CC_TYPE_NUMBER_SORT_TYPE (cc_number_sort_type_get_type ())

G_END_DECLS
