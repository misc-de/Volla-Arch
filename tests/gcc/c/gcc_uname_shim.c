/* LD_PRELOAD for the GNOME Control Center panel tests (tests/gcc): uname () names the kernel a
 * test asks for (PLINIUS_TEST_UNAME_RELEASE, PLINIUS_TEST_UNAME_VERSION), so that the pages
 * about the running kernel can be tested with the phone's kernel strings on any host. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdlib.h>
#include <string.h>
#include <sys/utsname.h>

int
uname (struct utsname *buf)
{
    int (*real) (struct utsname *) = (int (*) (struct utsname *)) dlsym (RTLD_NEXT, "uname");
    const char *release = getenv ("PLINIUS_TEST_UNAME_RELEASE");
    const char *version = getenv ("PLINIUS_TEST_UNAME_VERSION");
    int r = real (buf);

    if (r == 0 && release) {
        strncpy (buf->release, release, sizeof buf->release - 1);
        buf->release[sizeof buf->release - 1] = '\0';
    }
    if (r == 0 && version) {
        strncpy (buf->version, version, sizeof buf->version - 1);
        buf->version[sizeof buf->version - 1] = '\0';
    }
    return r;
}
