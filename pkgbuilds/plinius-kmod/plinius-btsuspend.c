// SPDX-License-Identifier: GPL-2.0-only
/*
 * plinius-btsuspend: no Bluetooth host suspend for the Volla Phone Plinius (ansuz)
 *
 * Bluetooth runs through bluebinder: the controller belongs to the Android HAL, the kernel only
 * sees a vhci device. Before every suspend the Bluetooth core (hci_suspend_notifier) sends HCI
 * commands to it: scanning off and, as vhci reports no wakeup support, a disconnect of every
 * link. bluebinder hands them to the MediaTek driver, which holds the wakeup source bt_drv_io
 * while it talks to the chip, and the freezing of tasks aborts. With Bluetooth on the phone never
 * suspended (368 of 368 attempts, docs/STANDBY.md), and a headset would lose its link on each
 * attempt. Android keeps the links and lets the controller wake the host instead.
 *
 * Bluetooth is built in and vhci cannot set HCI_QUIRK_NO_SUSPEND_NOTIFIER, so this module puts a
 * kprobe on the entry of hci_suspend_notifier and returns NOTIFY_DONE right away, for suspend
 * and resume alike. At the entry the return address is still in the link register (the function
 * starts with paciasp, the signed copy is never used), so the handler continues there.
 */
#include <linux/kprobes.h>
#include <linux/module.h>
#include <linux/notifier.h>
#include <linux/ptrace.h>

/* Notifier calls skipped since loading, for checking (read-only parameter) */
static unsigned int skipped;
module_param(skipped, uint, 0444);
MODULE_PARM_DESC(skipped, "Bluetooth suspend/resume notifier calls skipped");

static int plinius_btsuspend_pre(struct kprobe *p, struct pt_regs *regs)
{
	skipped++;
	regs->regs[0] = NOTIFY_DONE;
	instruction_pointer_set(regs, procedure_link_pointer(regs));
	/* Non-zero: the instruction is not single-stepped, execution goes on at the new pc */
	return 1;
}

static struct kprobe kp = {
	.symbol_name = "hci_suspend_notifier",
	.pre_handler = plinius_btsuspend_pre,
};

static int __init plinius_btsuspend_init(void)
{
	int ret = register_kprobe(&kp);

	if (ret)
		pr_err("plinius-btsuspend: kprobe on %s failed: %d\n", kp.symbol_name, ret);
	return ret;
}

static void __exit plinius_btsuspend_exit(void)
{
	unregister_kprobe(&kp);
}

module_init(plinius_btsuspend_init);
module_exit(plinius_btsuspend_exit);

MODULE_DESCRIPTION("No Bluetooth host suspend over vhci (Volla Phone Plinius)");
MODULE_LICENSE("GPL");
