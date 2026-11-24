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

/* Plinius: entry rows for the list style IPv4 and IPv6 pages. They check their text like
 * CEIPAddressEntry and CENetmaskEntry do, but are AdwEntryRows so that they fit a boxed list. */

#pragma once

#include <adwaita.h>

G_BEGIN_DECLS

/* An entry row for an IP address of @family (AF_INET or AF_INET6); empty is valid */
GtkWidget *ce_plinius_ip_row_new_address (int family, const gchar *title);

/* An entry row for an IPv4 netmask, either a prefix (0-32) or a dotted mask; empty is valid */
GtkWidget *ce_plinius_ip_row_new_netmask (const gchar *title);

/* A plain entry row for a number (IPv6 prefix, metric); it is checked by the page itself */
GtkWidget *ce_plinius_ip_row_new_number (const gchar *title);

gboolean ce_plinius_ip_row_is_empty (GtkWidget *row);

gboolean ce_plinius_ip_row_is_valid (GtkWidget *row);

/* The prefix of a netmask row, 0 if it is not valid */
guint32 ce_plinius_ip_row_get_prefix (GtkWidget *row);

/* A preferences group title (markup) from a label with a mnemonic, e.g. "IPv_4 Method" */
gchar *ce_plinius_group_title_from_mnemonic (const gchar *label);

G_END_DECLS
