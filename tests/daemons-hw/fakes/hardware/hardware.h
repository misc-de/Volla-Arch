/* Host stand-in for Android's hardware/hardware.h, as far as audio-hal-bridge and hybris-vulkan
 * use it (tests/daemons-hw). hw_get_module_by_class finds nothing: no HAL is ever loaded;
 * hw_get_module is only declared, a harness that needs it defines it. */
#ifndef FAKE_HARDWARE_HARDWARE_H
#define FAKE_HARDWARE_HARDWARE_H
#include <stdint.h>
#include <errno.h>

struct hw_module_t;
struct hw_device_t;
typedef struct hw_module_methods_t {
	int (*open)(const struct hw_module_t *module, const char *id, struct hw_device_t **device);
} hw_module_methods_t;
typedef struct hw_module_t {
	uint32_t tag;
	uint16_t module_api_version;
	uint16_t hal_api_version;
	const char *id;
	const char *name;
	struct hw_module_methods_t *methods;
} hw_module_t;
typedef struct hw_device_t {
	uint32_t tag, version;
	struct hw_module_t *module;
	int (*close)(struct hw_device_t *device);
} hw_device_t;

int hw_get_module(const char *id, const struct hw_module_t **module);

static inline int hw_get_module_by_class(const char *class_id, const char *inst, const struct hw_module_t **module)
{
	*module = 0;
	return -ENOENT;
}
#endif
