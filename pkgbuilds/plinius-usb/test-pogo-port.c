// SPDX-License-Identifier: MIT
// Checks the port decisions of pogo-port.h against the behaviour agreed for the Plinius Plus.
#include <stdio.h>
#include <string.h>

#include "pogo-port.h"

static int failures;

static void check(bool ok, const char *what)
{
	if (!ok) {
		printf("FAILED: %s\n", what);
		failures++;
	}
}

static bool same(const char *a, const char *b)
{
	return (!a && !b) || (a && b && !strcmp(a, b));
}

int main(void)
{
	/* Only the three states with both attached are a conflict */
	for (int flag = 0; flag <= 6; flag++)
		check(pogo_flag_is_conflict(flag) == (flag >= 1 && flag <= 3), "conflict states");

	check(pogo_flag_port(POGO_FLAG_NONE) == POGO_PORT_NONE, "nothing attached");
	check(pogo_flag_port(POGO_FLAG_USBC_DEVICE) == POGO_PORT_USBC, "computer only");
	check(pogo_flag_port(POGO_FLAG_USBC_HOST) == POGO_PORT_USBC, "stick only");
	check(pogo_flag_port(POGO_FLAG_POGO) == POGO_PORT_POGO, "accessory only");

	/* A question only for a real device on USB-C */
	check(!pogo_usbc_has_data(POGO_FLAG_BOTH_USBC_FIRST, false), "charger, then accessory: no question");
	check(!pogo_usbc_has_data(POGO_FLAG_BOTH_POGO_FIRST, false), "accessory, then charger: no question");
	check(pogo_usbc_has_data(POGO_FLAG_BOTH_USBC_FIRST, true), "computer, then accessory: question");
	check(pogo_usbc_has_data(POGO_FLAG_BOTH_POGO_FIRST, true), "accessory, then computer: question");
	check(pogo_usbc_has_data(POGO_FLAG_BOTH_USBC_HOST, false), "stick and accessory: question");

	/* No answer: what was active stays */
	check(pogo_port_without_answer(POGO_FLAG_BOTH_USBC_FIRST, POGO_PORT_USBC, true) == POGO_PORT_USBC,
	      "computer first, no answer: USB-C stays");
	check(pogo_port_without_answer(POGO_FLAG_BOTH_POGO_FIRST, POGO_PORT_POGO, true) == POGO_PORT_POGO,
	      "accessory first, no answer: pogo stays");
	check(pogo_port_without_answer(POGO_FLAG_BOTH_USBC_HOST, POGO_PORT_USBC, true) == POGO_PORT_USBC,
	      "stick first, no answer: USB-C stays");
	check(pogo_port_without_answer(POGO_FLAG_BOTH_USBC_HOST, POGO_PORT_POGO, true) == POGO_PORT_POGO,
	      "accessory first, then stick, no answer: pogo stays");
	check(pogo_port_without_answer(POGO_FLAG_BOTH_USBC_HOST, POGO_PORT_NONE, true) == POGO_PORT_USBC,
	      "unknown order: USB-C");

	/* No data partner: the accessory gets the port */
	check(pogo_port_without_answer(POGO_FLAG_BOTH_USBC_FIRST, POGO_PORT_USBC, false) == POGO_PORT_POGO,
	      "charger first: pogo");
	check(pogo_port_without_answer(POGO_FLAG_BOTH_POGO_FIRST, POGO_PORT_POGO, false) == POGO_PORT_POGO,
	      "accessory first, charger: pogo");

	/* The driver's values, per state (pogo_state_store) */
	check(same(pogo_port_value(POGO_FLAG_BOTH_USBC_FIRST, POGO_PORT_POGO), "1\n"), "USB-C first, take pogo");
	check(same(pogo_port_value(POGO_FLAG_BOTH_USBC_FIRST, POGO_PORT_USBC), "2\n"), "USB-C first, keep USB-C");
	check(same(pogo_port_value(POGO_FLAG_BOTH_POGO_FIRST, POGO_PORT_USBC), "1\n"), "pogo first, take USB-C");
	check(same(pogo_port_value(POGO_FLAG_BOTH_POGO_FIRST, POGO_PORT_POGO), NULL), "pogo first, keep pogo: nothing");
	check(same(pogo_port_value(POGO_FLAG_BOTH_USBC_HOST, POGO_PORT_POGO), "1\n"), "stick, take pogo");
	check(same(pogo_port_value(POGO_FLAG_BOTH_USBC_HOST, POGO_PORT_USBC), "2\n"), "stick, take USB-C");
	check(same(pogo_port_value(POGO_FLAG_POGO, POGO_PORT_POGO), NULL), "no conflict: never write");
	check(same(pogo_port_value(POGO_FLAG_USBC_DEVICE, POGO_PORT_USBC), NULL), "no conflict: never write");

	if (!failures)
		printf("All port decisions are as agreed\n");
	return failures ? 1 : 0;
}
