/* See cc-number-row-enums.h: glib-mkenums' output for cc-number-row.h, written by hand. */
#include "cc-number-row.h"
#include "cc-number-row-enums.h"

#define ENUM_TYPE(func, Name, ...)                                                    \
    GType func (void)                                                                 \
    {                                                                                 \
        static gsize type_id = 0;                                                     \
        static const GEnumValue values[] = { __VA_ARGS__, { 0, NULL, NULL } };        \
                                                                                      \
        if (g_once_init_enter (&type_id))                                             \
            g_once_init_leave (&type_id, g_enum_register_static (g_intern_static_string (Name), values)); \
        return type_id;                                                               \
    }

ENUM_TYPE (cc_number_order_get_type, "CcNumberOrder",
           { CC_NUMBER_ORDER_FIRST, "CC_NUMBER_ORDER_FIRST", "first" },
           { CC_NUMBER_ORDER_DEFAULT, "CC_NUMBER_ORDER_DEFAULT", "default" },
           { CC_NUMBER_ORDER_LAST, "CC_NUMBER_ORDER_LAST", "last" })

ENUM_TYPE (cc_number_value_type_get_type, "CcNumberValueType",
           { CC_NUMBER_VALUE_CUSTOM, "CC_NUMBER_VALUE_CUSTOM", "custom" },
           { CC_NUMBER_VALUE_STRING, "CC_NUMBER_VALUE_STRING", "string" },
           { CC_NUMBER_VALUE_SECONDS, "CC_NUMBER_VALUE_SECONDS", "seconds" },
           { CC_NUMBER_VALUE_MINUTES, "CC_NUMBER_VALUE_MINUTES", "minutes" },
           { CC_NUMBER_VALUE_HOURS, "CC_NUMBER_VALUE_HOURS", "hours" },
           { CC_NUMBER_VALUE_DAYS, "CC_NUMBER_VALUE_DAYS", "days" })

ENUM_TYPE (cc_number_sort_type_get_type, "CcNumberSortType",
           { CC_NUMBER_SORT_ASCENDING, "CC_NUMBER_SORT_ASCENDING", "ascending" },
           { CC_NUMBER_SORT_DESCENDING, "CC_NUMBER_SORT_DESCENDING", "descending" },
           { CC_NUMBER_SORT_NONE, "CC_NUMBER_SORT_NONE", "none" })
