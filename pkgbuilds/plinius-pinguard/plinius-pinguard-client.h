/*
 * plinius-pinguard-client.h: asking plinius-pinguard (see plinius-pinguard.h), for the PAM
 * modules and tools of this package and of plinius-pattern. Plain C, no GLib.
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include <errno.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

#include "plinius-pinguard.h"

/* The check hashes with yescrypt and the daemon checks one request at a time, each in at most
 * a few seconds */
#define PINGUARD_ANSWER_TIMEOUT 30

/*
 * Sends one request (@kind NULL for a plain password check) to the daemon at @path and reads
 * its one-line answer, without the newline, into @answer. Returns 0, or -1 with errno set if the
 * daemon cannot be reached or does not answer: the caller decides what that means.
 */
static inline int
pinguard_ask (const char *path, const char *user, const char *password, const char *kind,
              char *answer, size_t size)
{
    struct sockaddr_un addr = { .sun_family = AF_UNIX };
    struct timeval timeout = { PINGUARD_ANSWER_TIMEOUT, 0 };
    size_t ulen = strlen (user) + 1, plen = strlen (password) + 1, got = 0;
    size_t klen = kind ? strlen (kind) + 1 : 0, total = ulen + plen + klen;
    char *request;
    bool sent;
    int fd, saved;

    if (size == 0 || strlen (path) >= sizeof addr.sun_path || total > REQUEST_MAX) {
        errno = EINVAL;
        return -1;
    }
    strcpy (addr.sun_path, path);

    fd = socket (AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0)
        return -1;
    setsockopt (fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof timeout);
    setsockopt (fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof timeout);
    if (connect (fd, (struct sockaddr *) &addr, sizeof addr) < 0) {
        saved = errno;
        close (fd);
        errno = saved;
        return -1;
    }

    request = malloc (total);
    if (!request) {
        close (fd);
        errno = ENOMEM;
        return -1;
    }
    memcpy (request, user, ulen);
    memcpy (request + ulen, password, plen);
    if (kind)
        memcpy (request + ulen + plen, kind, klen);
    sent = send (fd, request, total, MSG_NOSIGNAL) == (ssize_t) total;
    explicit_bzero (request, total);
    free (request);
    shutdown (fd, SHUT_WR);

    while (sent && got < size - 1) {
        ssize_t r = read (fd, answer + got, size - 1 - got);

        if (r <= 0)
            break;
        got += r;
        if (memchr (answer, '\n', got))
            break;
    }
    close (fd);
    answer[got] = '\0';
    answer[strcspn (answer, "\n")] = '\0';

    if (!sent || got == 0) {
        errno = ETIMEDOUT;
        return -1;
    }
    return 0;
}

/*
 * Whether the errno of a failed pinguard_ask() means there is no daemon at all (its socket is
 * missing or nobody listens on it), as opposed to one that did not answer in time. Only the first
 * may fall back to a check without the count: anyone on the phone can open connections to the
 * world-writable socket, so a slow answer must never open the uncounted way.
 */
static inline bool
pinguard_unreachable (int err)
{
    return err == ENOENT || err == ECONNREFUSED || err == ENOTSOCK;
}

/* "45 seconds", "5 minutes", "1 hour": rounded up, so the user never tries too early */
static inline void
pinguard_format_wait (long seconds, char *buf, size_t size)
{
    if (seconds < 60)
        snprintf (buf, size, "%ld second%s", seconds, seconds == 1 ? "" : "s");
    else if (seconds < 60 * 60) {
        long minutes = (seconds + 59) / 60;
        snprintf (buf, size, "%ld minute%s", minutes, minutes == 1 ? "" : "s");
    } else {
        long hours = (seconds + 60 * 60 - 1) / (60 * 60);
        snprintf (buf, size, "%ld hour%s", hours, hours == 1 ? "" : "s");
    }
}
