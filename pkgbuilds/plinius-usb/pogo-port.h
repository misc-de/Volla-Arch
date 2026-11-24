// SPDX-License-Identifier: MIT
// Which USB port carries data on the Plinius Plus: the pure decisions, without any I/O, so that
// test-pogo-port.c can check them. The driver (mtk-pogo-usb) reports its state in
// /sys/usb/pogo_state and has a single USB controller behind a switch: USB-C or the pogo pins.
#ifndef POGO_PORT_H
#define POGO_PORT_H

#include <stdbool.h>
#include <stddef.h>

/* What /sys/usb/pogo_state reads */
enum {
	POGO_FLAG_NONE = 0,	     /* Nothing attached */
	POGO_FLAG_BOTH_USBC_FIRST = 1,  /* USB-C gets power, it was there before the accessory */
	POGO_FLAG_BOTH_POGO_FIRST = 2,  /* USB-C gets power, the accessory was there before it */
	POGO_FLAG_BOTH_USBC_HOST = 3,   /* The phone powers a device on USB-C, plus the accessory */
	POGO_FLAG_USBC_HOST = 4,	/* Only a device on USB-C, powered by the phone */
	POGO_FLAG_POGO = 5,	     /* Only the accessory */
	POGO_FLAG_USBC_DEVICE = 6,      /* Only USB-C, the phone gets power */
};

typedef enum {
	POGO_PORT_NONE,
	POGO_PORT_USBC,
	POGO_PORT_POGO,
} PogoPort;

static inline const char *pogo_port_name(PogoPort port)
{
	return port == POGO_PORT_USBC ? "usb-c" : port == POGO_PORT_POGO ? "pogo" : "none";
}

/* Both are attached: the driver waits 10 seconds for a choice, then takes the pogo pins */
static inline bool pogo_flag_is_conflict(int flag)
{
	return flag == POGO_FLAG_BOTH_USBC_FIRST || flag == POGO_FLAG_BOTH_POGO_FIRST ||
	       flag == POGO_FLAG_BOTH_USBC_HOST;
}

/* The port of a state without a conflict */
static inline PogoPort pogo_flag_port(int flag)
{
	if (flag == POGO_FLAG_USBC_HOST || flag == POGO_FLAG_USBC_DEVICE)
		return POGO_PORT_USBC;
	return flag == POGO_FLAG_POGO ? POGO_PORT_POGO : POGO_PORT_NONE;
}

/* Which port was active before the conflict. `before` only decides the case the flag can't tell,
 * USB-C if that is unknown as well (both already there when the service started). */
static inline PogoPort pogo_port_before(int flag, PogoPort before)
{
	if (flag == POGO_FLAG_BOTH_USBC_FIRST)
		return POGO_PORT_USBC;
	if (flag == POGO_FLAG_BOTH_POGO_FIRST)
		return POGO_PORT_POGO;
	return before != POGO_PORT_NONE ? before : POGO_PORT_USBC;
}

/* Is the other end of USB-C worth a question? A device the phone powers always is. Otherwise a
 * computer: a wall charger has no data, the accessory just gets the port. */
static inline bool pogo_usbc_has_data(int flag, bool computer)
{
	return flag == POGO_FLAG_BOTH_USBC_HOST || computer;
}

/* Without an answer nothing changes, unless there's no data partner to lose */
static inline PogoPort pogo_port_without_answer(int flag, PogoPort before, bool has_data)
{
	return has_data ? pogo_port_before(flag, before) : POGO_PORT_POGO;
}

/* What makes the driver take a port, NULL if it does so on its own.
 *
 * The meaning of the values depends on the state (pogo_state_store() in the driver). With the
 * accessory first and power from USB-C, "pogo" stays the driver's own way after the timeout: it
 * is the one that switches the phone's 5 V off, the cable provides them now. Value 2 would leave
 * the phone's boost converter running against the cable. */
static inline const char *pogo_port_value(int flag, PogoPort port)
{
	switch (flag) {
	case POGO_FLAG_BOTH_USBC_FIRST:
	case POGO_FLAG_BOTH_USBC_HOST:
		return port == POGO_PORT_POGO ? "1\n" : "2\n";
	case POGO_FLAG_BOTH_POGO_FIRST:
		return port == POGO_PORT_USBC ? "1\n" : NULL;
	default:
		return NULL;
	}
}

#endif
