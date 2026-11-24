/* Host stand-in for libhybris' dlfcn (tests/daemons-hw): no Android library is ever loaded.
 * A harness that defines FAKE_HYBRIS_HOOKS provides fake_hybris_dlopen/_dlsym/_dlerror itself
 * and so hands out its own stand-ins for the Android functions; without it nothing is found. */
#ifndef FAKE_HYBRIS_COMMON_DLFCN_H
#define FAKE_HYBRIS_COMMON_DLFCN_H
#ifdef FAKE_HYBRIS_HOOKS
void *fake_hybris_dlopen(const char *name, int flags);
void *fake_hybris_dlsym(void *handle, const char *name);
const char *fake_hybris_dlerror(void);
#endif
static inline void *hybris_dlopen(const char *name, int flags)
{
#ifdef FAKE_HYBRIS_HOOKS
	return fake_hybris_dlopen(name, flags);
#else
	return 0;
#endif
}
static inline void *hybris_dlsym(void *handle, const char *name)
{
#ifdef FAKE_HYBRIS_HOOKS
	return fake_hybris_dlsym(handle, name);
#else
	return 0;
#endif
}
static inline const char *hybris_dlerror(void)
{
#ifdef FAKE_HYBRIS_HOOKS
	return fake_hybris_dlerror();
#else
	return "no libhybris on the host";
#endif
}
#endif
