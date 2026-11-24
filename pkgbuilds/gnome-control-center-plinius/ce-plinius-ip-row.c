/* -*- Mode: C; tab-width: 8; indent-tabs-mode: nil; c-basic-offset: 8 -*-
 *
 * Licensed under the GNU General Public License Version 2
 *
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 2 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 */

#include "config.h"

#include <NetworkManager.h>
#include <arpa/inet.h>
#include <errno.h>
#include <glib/gi18n.h>
#include <stdlib.h>
#include <string.h>

#include "ce-plinius-ip-row.h"
#include "ui-helpers.h"

/* Plinius: what a row holds; addresses store their family (AF_INET, AF_INET6) */
#define KIND_KEY "ce-plinius-ip-row-kind"
#define KIND_NETMASK -1

/* Plinius: the same rules as parse_netmask() in ce-netmask-entry.c */
static gboolean
parse_netmask (const char *str, guint32 *prefix)
{
    struct in_addr tmp_addr;
    glong tmp_prefix;
    char *end = NULL;

    /* Is it a prefix? Digits only: strtol () alone takes "abc" as 0 and "24x" as 24 */
    errno = 0;
    if (!strchr (str, '.') && g_ascii_isdigit (str[0])) {
        tmp_prefix = strtol (str, &end, 10);
        if (!errno && *end == '\0' && tmp_prefix >= 0 && tmp_prefix <= 32) {
            if (prefix != NULL)
                *prefix = tmp_prefix;
            return TRUE;
        }
    }

    /* Is it a netmask? */
    if (inet_pton (AF_INET, str, &tmp_addr) > 0) {
        if (prefix != NULL)
            *prefix = nm_utils_ip4_netmask_to_prefix (tmp_addr.s_addr);
        return TRUE;
    }

    return FALSE;
}

static gint
get_kind (GtkWidget *row)
{
    return GPOINTER_TO_INT (g_object_get_data (G_OBJECT (row), KIND_KEY));
}

static void
on_row_changed (GtkEditable *editable)
{
    GtkWidget *row = GTK_WIDGET (editable);

    if (ce_plinius_ip_row_is_valid (row))
        widget_unset_error (row);
    else
        widget_set_error (row);
}

static void
on_row_focus (GtkEventControllerFocus *controller, GtkWidget *row)
{
    /* As in CEIPAddressEntry and CENetmaskEntry: tell only when the user enters or leaves the
     * row, not after each keystroke */
    if (ce_plinius_ip_row_is_valid (row))
        return;

    gtk_accessible_announce (GTK_ACCESSIBLE (row),
                             get_kind (row) == KIND_NETMASK ? _("Netmask value not valid")
                                                            : _("IP address value not valid"),
                             GTK_ACCESSIBLE_ANNOUNCEMENT_PRIORITY_HIGH);
}

static GtkWidget *
new_row (const gchar *title, GtkInputPurpose purpose)
{
    GtkWidget *row;

    row = adw_entry_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    adw_entry_row_set_input_purpose (ADW_ENTRY_ROW (row), purpose);
    adw_entry_row_set_input_hints (ADW_ENTRY_ROW (row), GTK_INPUT_HINT_NO_EMOJI | GTK_INPUT_HINT_NO_SPELLCHECK);

    return row;
}

static GtkWidget *
new_checked_row (const gchar *title, gint kind)
{
    GtkWidget *row;
    GtkEventController *controller;

    /* Free form: the dots and colons of an address are not on a digits keyboard */
    row = new_row (title, GTK_INPUT_PURPOSE_FREE_FORM);
    g_object_set_data (G_OBJECT (row), KIND_KEY, GINT_TO_POINTER (kind));
    g_signal_connect (row, "changed", G_CALLBACK (on_row_changed), NULL);

    controller = gtk_event_controller_focus_new ();
    g_signal_connect (controller, "enter", G_CALLBACK (on_row_focus), row);
    g_signal_connect (controller, "leave", G_CALLBACK (on_row_focus), row);
    gtk_widget_add_controller (row, controller);

    return row;
}

GtkWidget *
ce_plinius_ip_row_new_address (int family, const gchar *title)
{
    g_return_val_if_fail (family == AF_INET || family == AF_INET6, NULL);

    return new_checked_row (title, family);
}

GtkWidget *
ce_plinius_ip_row_new_netmask (const gchar *title)
{
    return new_checked_row (title, KIND_NETMASK);
}

GtkWidget *
ce_plinius_ip_row_new_number (const gchar *title)
{
    return new_row (title, GTK_INPUT_PURPOSE_DIGITS);
}

gboolean
ce_plinius_ip_row_is_empty (GtkWidget *row)
{
    g_return_val_if_fail (ADW_IS_ENTRY_ROW (row), FALSE);

    return gtk_editable_get_text (GTK_EDITABLE (row))[0] == '\0';
}

gboolean
ce_plinius_ip_row_is_valid (GtkWidget *row)
{
    const gchar *text;
    gint kind;

    g_return_val_if_fail (ADW_IS_ENTRY_ROW (row), FALSE);

    text = gtk_editable_get_text (GTK_EDITABLE (row));
    if (text[0] == '\0')
        return TRUE;

    kind = get_kind (row);
    if (kind == KIND_NETMASK)
        return parse_netmask (text, NULL);
    if (kind == AF_INET || kind == AF_INET6)
        return nm_utils_ipaddr_valid (kind, text);

    return TRUE;
}

guint32
ce_plinius_ip_row_get_prefix (GtkWidget *row)
{
    guint32 prefix = 0;

    g_return_val_if_fail (ADW_IS_ENTRY_ROW (row), 0);

    parse_netmask (gtk_editable_get_text (GTK_EDITABLE (row)), &prefix);

    return prefix;
}

gchar *
ce_plinius_group_title_from_mnemonic (const gchar *label)
{
    g_autoptr(GString) plain = NULL;
    const gchar *p;

    g_return_val_if_fail (label != NULL, NULL);

    /* Group titles have no mnemonics: drop the underscore, keep a doubled one as one */
    plain = g_string_sized_new (strlen (label));
    for (p = label; *p; p++) {
        if (*p == '_') {
            if (p[1] != '_')
                continue;
            p++;
        }
        g_string_append_c (plain, *p);
    }

    return g_markup_escape_text (plain->str, -1);
}
