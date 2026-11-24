// SPDX-License-Identifier: GPL-2.0-only
/*
 * plinius-novt: no VT switch on suspend for the Volla Phone Plinius (ansuz)
 *
 * Before a suspend the kernel switches to its own console (pm_prepare_console) unless a driver
 * has said it does not need that. No driver of this kernel says so. For logind the graphical
 * session then becomes inactive for the length of the suspend: it pauses the seat, phoc gives up
 * the DRM device, and on resume the session coming back switches the display on again, even
 * when a modem interrupt woke the phone with the display off (docs/STANDBY.md).
 *
 * This module registers a placeholder device that declares the switch not required. With an
 * entry in the list and none requiring it, pm_vt_switch() skips the switch.
 */
#include <linux/err.h>
#include <linux/module.h>
#include <linux/platform_device.h>
#include <linux/pm.h>

static struct platform_device *pdev;

static int __init plinius_novt_init(void)
{
	pdev = platform_device_register_simple("plinius-novt", PLATFORM_DEVID_NONE, NULL, 0);
	if (IS_ERR(pdev))
		return PTR_ERR(pdev);
	pm_vt_switch_required(&pdev->dev, false);
	return 0;
}

static void __exit plinius_novt_exit(void)
{
	pm_vt_switch_unregister(&pdev->dev);
	platform_device_unregister(pdev);
}

module_init(plinius_novt_init);
module_exit(plinius_novt_exit);

MODULE_DESCRIPTION("No VT switch on suspend (Volla Phone Plinius)");
MODULE_LICENSE("GPL");
