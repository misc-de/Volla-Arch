// SPDX-License-Identifier: GPL-2.0-only
/*
 * plinius-backlight: backlight class device for the Volla Phone Plinius (ansuz)
 *
 * The MediaTek display driver offers the panel brightness only as the LED class device
 * "lcd-backlight" (CONFIG_BACKLIGHT_LED is off in the kernel). Shells like Phosh look for a
 * device in the backlight class, so this module registers one ("platform" type, preferred
 * for built-in panels) and forwards its brightness to the LED.
 *
 * The LED takes 0..max_brightness, but the panel is dark below min_led (the hardware
 * minimum in the device tree). The backlight range starts there, so 0 is the dimmest
 * visible level and never switches the panel off; that stays with DRM (DPMS).
 */
#include <linux/backlight.h>
#include <linux/leds.h>
#include <linux/module.h>
#include <linux/rwsem.h>
#include <linux/string.h>

/* Exported by the LED core, declared only in its private header (drivers/leds/leds.h) */
extern struct rw_semaphore leds_list_lock;
extern struct list_head leds_list;

static char *led_name = "lcd-backlight";
module_param(led_name, charp, 0444);
MODULE_PARM_DESC(led_name, "LED class device of the panel (default: lcd-backlight)");

static unsigned int min_led = 4;
module_param(min_led, uint, 0444);
MODULE_PARM_DESC(min_led, "Lowest LED brightness at which the panel is still lit (default: 4)");

static struct backlight_device *bl;
static struct led_classdev *led;
/* Driver module of the LED, held while we point at its led_classdev */
static struct module *led_owner;

static int plinius_bl_update_status(struct backlight_device *bd)
{
	led_set_brightness(bl_get_data(bd), min_led + bd->props.brightness);
	return 0;
}

static const struct backlight_ops plinius_bl_ops = {
	.update_status = plinius_bl_update_status,
};

/* Finds the LED by name and pins the module of its driver */
static struct led_classdev *find_led(void)
{
	struct led_classdev *cdev, *found = NULL;

	down_read(&leds_list_lock);
	list_for_each_entry(cdev, &leds_list, node) {
		struct device *parent = cdev->dev ? cdev->dev->parent : NULL;

		if (strcmp(cdev->name, led_name))
			continue;
		led_owner = parent && parent->driver ? parent->driver->owner : NULL;
		if (led_owner && !try_module_get(led_owner))
			break;
		found = cdev;
		break;
	}
	up_read(&leds_list_lock);
	return found;
}

static int __init plinius_bl_init(void)
{
	struct backlight_properties props = {
		.type = BACKLIGHT_PLATFORM,
		.scale = BACKLIGHT_SCALE_NON_LINEAR,
	};

	led = find_led();
	if (!led) {
		pr_err("plinius-backlight: LED %s not found\n", led_name);
		return -ENODEV;
	}
	if (led->max_brightness <= min_led) {
		pr_err("plinius-backlight: LED %s has only %u levels\n", led_name, led->max_brightness);
		module_put(led_owner);
		return -EINVAL;
	}

	props.max_brightness = led->max_brightness - min_led;
	props.brightness = led->brightness > min_led ? led->brightness - min_led : 0;
	bl = backlight_device_register("plinius-backlight", led->dev->parent, led, &plinius_bl_ops,
				       &props);
	if (IS_ERR(bl)) {
		module_put(led_owner);
		return PTR_ERR(bl);
	}
	pr_info("plinius-backlight: forwarding to LED %s (%u..%u)\n", led_name, min_led,
		led->max_brightness);
	return 0;
}

static void __exit plinius_bl_exit(void)
{
	backlight_device_unregister(bl);
	module_put(led_owner);
}

module_init(plinius_bl_init);
module_exit(plinius_bl_exit);

MODULE_DESCRIPTION("Backlight class device on top of the panel LED (Volla Phone Plinius)");
MODULE_AUTHOR("misc-de <misc-de@users.noreply.github.com>");
MODULE_LICENSE("GPL");
