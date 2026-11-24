// SPDX-License-Identifier: BSD-2-Clause
// Reserve the TLS slots that bionic code reads straight from the thread pointer.
//
// On aarch64, Android libraries (Mali's libGLES_mali.so among them) access
// tpidr_el0[2..7] (TLS_SLOT_APP .. TLS_SLOT_ART_THREAD_SELF) with inline code,
// e.g. the current GL context in TLS_SLOT_OPENGL. Under glibc these bytes are the
// start of the static TLS, i.e. the TLS block of the first module that has one.
// Its variables and the bionic slots then share memory and overwrite each other:
// phoc crashed in glBindTexture because a glibc library had replaced the context.
//
// Preloaded (/etc/ld.so.preload), this library comes right after the executable,
// so its block is the first one and covers exactly these slots. That does not hold
// for executables with TLS of their own; their block comes first.
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#define FIRST_SLOT 2
#define LAST_SLOT 7

__attribute__((used, aligned(16)))
static __thread void *bionic_slots[LAST_SLOT - FIRST_SLOT + 1];

// Loaded into every process: report a misplaced block only on request
__attribute__((constructor))
static void check_layout(void)
{
	if (!getenv("HYBRIS_TLS_PADDING_DEBUG"))
		return;
	intptr_t offset = (char *)&bionic_slots[0] - (char *)__builtin_thread_pointer();
	if (offset == FIRST_SLOT * (intptr_t)sizeof(void *))
		fprintf(stderr, "hybris-tls-padding: bionic TLS slots %d-%d reserved\n", FIRST_SLOT, LAST_SLOT);
	else
		fprintf(stderr, "hybris-tls-padding: slots at offset %ld instead of %ld, "
			"bionic TLS slots are not protected\n",
			(long)offset, (long)(FIRST_SLOT * sizeof(void *)));
}
