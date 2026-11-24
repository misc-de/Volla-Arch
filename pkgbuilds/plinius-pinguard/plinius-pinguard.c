/*
 * plinius-pinguard: checks login passwords (on the phone the PIN) for pam_plinius_pinguard and
 * makes every wrong one after the first few cost waiting time (see plinius-pinguard.h).
 *
 * Runs as root, socket-activated (plinius-pinguard.socket hands over fd 3), and exits after
 * IDLE_EXIT seconds without a request. Requests are read from up to MAX_CLIENTS connections at
 * once, but checked one at a time, so tries are strictly one after the other. A client gets
 * READ_TIMEOUT seconds in all to send its request. A client that connects and sends nothing
 * holds up no one: the socket is world-writable, and while one connection was served at a time,
 * a few silent ones from any local account kept the lock screen's request from being answered,
 * which made its PAM module fall back to pam_unix and its count-free check. When all slots are
 * taken, the oldest connection that has not sent its request yet is dropped for the new one.
 * The counter of a user is written before the answer goes out: a caller that hangs up early
 * still pays for its try.
 *
 * Without the marker PINGUARD_ENABLED the wait is off: the password is still checked here, but
 * neither counted nor delayed, and old counters are ignored. That is for logins at the phone:
 * those over a network and tries from another local account (see plinius-pinguard.h) are always
 * counted, each in a file of their own:
 *   <uid>               at the phone (lock screen, sudo, polkit as the user itself)
 *   <uid>.network       over a network (VNC)
 *   <uid>.from-<uid2>   in a program the other local user uid2 started (su, polkit's helper)
 *
 * For tests, on a socket, state directory and marker of its own, and as if in another boot:
 *   plinius-pinguard --listen PATH --state DIR [--enabled FILE] [--boot-id ID]
 *
 * SPDX-License-Identifier: MIT
 */

#define _GNU_SOURCE
#include <crypt.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <pwd.h>
#include <shadow.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/un.h>
#include <syslog.h>
#include <time.h>
#include <unistd.h>

#include "plinius-pinguard.h"

#define IDLE_EXIT 60
/* A client gets this long in all to send its request */
#define READ_TIMEOUT 5
/* Connections read at the same time */
#define MAX_CLIENTS 32
/* "36 characters" of /proc/sys/kernel/random/boot_id */
#define BOOT_ID_SIZE 37

static const char *state_dir = PINGUARD_STATE_DIR;
static const char *enabled_file = PINGUARD_ENABLED;
/* This boot, to tell a wait of this boot from one before a restart */
static char boot_id[BOOT_ID_SIZE];

/* Failures in a row and the time (seconds since boot, CLOCK_BOOTTIME) before which nothing is
 * checked */
typedef struct {
    unsigned failures;
    long long next_try;
} State;

/* Seconds since boot, suspend included */
static long long
now_since_boot (void)
{
    struct timespec ts;

    clock_gettime (CLOCK_BOOTTIME, &ts);
    return ts.tv_sec;
}

static void
read_boot_id (void)
{
    FILE *f = fopen ("/proc/sys/kernel/random/boot_id", "re");

    if (!f || !fgets (boot_id, sizeof boot_id, f))
        boot_id[0] = '\0';
    boot_id[strcspn (boot_id, "\n")] = '\0';
    if (f)
        fclose (f);
}

/* @suffix: "" at the phone, ".network", ".from-<uid>" (see above) */
static void
state_path (uid_t uid, const char *suffix, char *path, size_t size)
{
    snprintf (path, size, "%s/%u%s", state_dir, (unsigned) uid, suffix);
}

static bool save_state (uid_t uid, const char *suffix, State state);

/*
 * The file holds "2 <failures> <boot id> <next try>". A wait from before a restart (another boot
 * id, or the first format "<failures> <epoch seconds>") starts over in full now, and is written
 * back at once: else every try during it would start it over again.
 */
static State
load_state (uid_t uid, const char *suffix, long long now)
{
    State state = { 0, 0 };
    char path[512], line[128], saved_boot[BOOT_ID_SIZE];
    unsigned version;
    bool this_boot = false;
    FILE *f;

    state_path (uid, suffix, path, sizeof path);
    f = fopen (path, "re");
    if (!f)
        return state;
    if (!fgets (line, sizeof line, f))
        line[0] = '\0';
    fclose (f);

    if (sscanf (line, "%u %u %36s %lld", &version, &state.failures, saved_boot, &state.next_try) == 4 &&
        version == 2) {
        if (boot_id[0])
            this_boot = strcmp (saved_boot, boot_id) == 0;
        else
            /* No boot id to tell a restart by (saved as "-"): a wait that ends no later than a
             * full one from now may be this boot's; else it started over at every try, and after
             * the first wait the account stayed locked */
            this_boot = strcmp (saved_boot, "-") == 0 && state.failures &&
                        state.next_try <= now + pinguard_wait_after (state.failures);
    } else if (sscanf (line, "%u %lld", &state.failures, &state.next_try) != 2)
        return (State) { 0, 0 };

    if (!this_boot && state.failures) {
        state.next_try = now + pinguard_wait_after (state.failures);
        save_state (uid, suffix, state);
    }
    return state;
}

/* Written to a temporary file and renamed, so a crash leaves the old or the new counter */
static bool
save_state (uid_t uid, const char *suffix, State state)
{
    char path[512], tmp[520];
    FILE *f;
    bool ok;

    state_path (uid, suffix, path, sizeof path);
    snprintf (tmp, sizeof tmp, "%s.tmp", path);
    f = fopen (tmp, "we");
    if (!f) {
        syslog (LOG_ERR, "cannot write %s: %m", tmp);
        return false;
    }
    fprintf (f, "2 %u %s %lld\n", state.failures, boot_id[0] ? boot_id : "-", state.next_try);
    ok = fflush (f) == 0 && fsync (fileno (f)) == 0;
    ok = fclose (f) == 0 && ok;
    if (!ok || rename (tmp, path) < 0) {
        syslog (LOG_ERR, "cannot write %s: %m", path);
        unlink (tmp);
        return false;
    }
    return true;
}

/* The password against the user's /etc/shadow hash, hashed with libcrypt like pam_unix */
static bool
password_matches (const char *user, const char *password)
{
    struct crypt_data *data;
    struct spwd *sp = getspnam (user);
    const char *hash;
    bool match;

    /* No hash, or a locked account ("!", "*"): nothing opens it here */
    if (!sp || !sp->sp_pwdp || sp->sp_pwdp[0] == '\0' || sp->sp_pwdp[0] == '!' || sp->sp_pwdp[0] == '*')
        return false;

    data = calloc (1, sizeof *data);
    if (!data)
        return false;
    hash = crypt_r (password, sp->sp_pwdp, data);
    match = hash && hash[0] != '*' && strcmp (hash, sp->sp_pwdp) == 0;
    explicit_bzero (data, sizeof *data);
    free (data);
    return match;
}

static size_t
count_nuls (const char *buf, size_t size)
{
    size_t n = 0;

    for (size_t i = 0; i < size; i++)
        n += buf[i] == '\0';
    return n;
}

/*
 * The uid of a kind "from:UID" (decimal, no sign, no leading zero, below 2^31), or -1 if @kind is
 * not of that form. Strict, so that one try can't be filed under several names.
 */
static long long
parse_from (const char *kind)
{
    const char *digits;
    long long uid = 0;

    if (strncmp (kind, PINGUARD_FROM, strlen (PINGUARD_FROM)) != 0)
        return -1;
    digits = kind + strlen (PINGUARD_FROM);
    if (digits[0] < '1' || digits[0] > '9')
        return -1;
    for (const char *c = digits; *c; c++) {
        if (*c < '0' || *c > '9' || c - digits >= 10)
            return -1;
        uid = uid * 10 + (*c - '0');
    }
    return uid < 0x7fffffffLL ? uid : -1;
}

static void
answer (int fd, const char *line)
{
    ssize_t r = write (fd, line, strlen (line));

    (void) r;
}

/* Answers the request @buf (@got bytes, NUL-terminated after them) that came from @cred on @fd */
static void
handle_request (int fd, struct ucred cred, char *buf, size_t got)
{
    char line[64], suffix[32] = "";
    const char *user, *password, *kind = NULL;
    struct passwd *pw;
    long long now, from = -1;
    State state;
    bool network = false, attempt = false, success = false;

    /* "user\0password\0" and maybe a kind, e.g. "network\0" (see plinius-pinguard.h) */
    user = buf;
    password = count_nuls (buf, got) >= 2 ? buf + strlen (buf) + 1 : NULL;
    if (!password || user[0] == '\0') {
        answer (fd, "ERROR\n");
        goto out;
    }
    if (count_nuls (buf, got) >= 3)
        kind = password + strlen (password) + 1;
    if (kind) {
        network = strcmp (kind, PINGUARD_NETWORK) == 0;
        attempt = strcmp (kind, PINGUARD_ATTEMPT) == 0;
        success = strcmp (kind, PINGUARD_SUCCESS) == 0;
        from = parse_from (kind);
        /* The pattern is checked by the caller; it has no password to send */
        if (!network && !attempt && !success && from < 0) {
            answer (fd, "ERROR\n");
            goto out;
        }
        if ((attempt || success) && password[0] != '\0') {
            answer (fd, "ERROR\n");
            goto out;
        }
    }

    pw = getpwnam (user);
    if (!pw) {
        answer (fd, "ERROR\n");
        goto out;
    }

    /* Only root (sudo, polkit's helper) or the user itself may try this user's password */
    if (cred.uid != 0 && cred.uid != pw->pw_uid) {
        syslog (LOG_WARNING, "uid %u asked for the password of %s", (unsigned) cred.uid, user);
        answer (fd, "DENIED\n");
        goto out;
    }

    /* Which account a try comes from is told only by root: a setuid program that loaded the PAM
     * module, which takes it from its real uid. Anyone else could file its tries under a counter
     * of its choice. From the user itself (plinius in its own polkit dialog) it is a try at the
     * phone as without "from:"; root is never sent (parse_from refuses 0). */
    if (from >= 0) {
        if (cred.uid != 0) {
            syslog (LOG_WARNING, "uid %u sent a try from another account", (unsigned) cred.uid);
            answer (fd, "DENIED\n");
            goto out;
        }
        if (from == (long long) pw->pw_uid)
            from = -1;
        else
            snprintf (suffix, sizeof suffix, ".from-%lld", from);
    } else if (network) {
        snprintf (suffix, sizeof suffix, ".network");
    }

    /* Wait switched off: a plain check, nothing counted, no running wait honoured. Not for a
     * network login and not for a try from another account: the wait there can't be switched
     * off (the switch is the owner's, not the other account's). */
    if (!network && from < 0 && access (enabled_file, F_OK) < 0) {
        if (attempt)
            answer (fd, "COUNTED 0 0\n");
        else if (success)
            answer (fd, "OK\n");
        else
            answer (fd, password_matches (user, password) ? "OK\n" : "FAIL 0 0\n");
        goto out;
    }

    now = now_since_boot ();
    state = load_state (pw->pw_uid, suffix, now);

    /* A right pattern: the try counted for it was not a wrong one */
    if (success) {
        if (state.failures && !save_state (pw->pw_uid, suffix, (State) { 0, 0 })) {
            answer (fd, "ERROR\n");
            goto out;
        }
        answer (fd, "OK\n");
        goto out;
    }

    if (now < state.next_try) {
        snprintf (line, sizeof line, "WAIT %lld\n", state.next_try - now);
        answer (fd, line);
        goto out;
    }

    /* A pattern try, counted as wrong until the caller reports a success */
    if (attempt) {
        long wait;

        state.failures++;
        wait = pinguard_wait_after (state.failures);
        state.next_try = now + wait;
        if (!save_state (pw->pw_uid, suffix, state)) {
            answer (fd, "ERROR\n");
            goto out;
        }
        snprintf (line, sizeof line, "COUNTED %u %ld\n", state.failures, wait);
        answer (fd, line);
        goto out;
    }

    if (password_matches (user, password)) {
        if (state.failures)
            save_state (pw->pw_uid, suffix, (State) { 0, 0 });
        answer (fd, "OK\n");
    } else {
        long wait;

        state.failures++;
        wait = pinguard_wait_after (state.failures);
        state.next_try = now + wait;
        /* Counted before the answer: hanging up does not save the try */
        if (!save_state (pw->pw_uid, suffix, state)) {
            answer (fd, "ERROR\n");
            goto out;
        }
        if (from >= 0)
            syslog (LOG_NOTICE, "wrong password for %s from uid %lld, %u in a row, next try in %ld s",
                    user, from, state.failures, wait);
        else
            syslog (LOG_NOTICE, "wrong password for %s%s, %u in a row, next try in %ld s", user,
                    network ? " over the network" : "", state.failures, wait);
        snprintf (line, sizeof line, "FAIL %u %ld\n", state.failures, wait);
        answer (fd, line);
    }

out:
    explicit_bzero (buf, got);
}

/* A connection whose request is still coming in */
typedef struct {
    int fd;                 /* -1: slot free */
    struct ucred cred;
    long long deadline_ms;  /* CLOCK_MONOTONIC */
    size_t got;
    char buf[REQUEST_MAX + 1];
} Client;

static Client clients[MAX_CLIENTS];

static long long
now_ms (void)
{
    struct timespec ts;

    clock_gettime (CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000LL + ts.tv_nsec / 1000000;
}

static void
drop_client (Client *c)
{
    close (c->fd);
    explicit_bzero (c->buf, c->got);
    c->fd = -1;
    c->got = 0;
}

/* Checks the request of @c, answers it and closes the connection */
static void
finish_client (Client *c)
{
    c->buf[c->got] = '\0';
    handle_request (c->fd, c->cred, c->buf, c->got);
    drop_client (c);
}

/*
 * Reads what @c has sent so far. The request is complete at the end of the stream (the client
 * shuts down its writing side), at the third NUL or at the size limit; then it is checked at
 * once, before the next connection is read.
 */
static void
read_client (Client *c)
{
    for (;;) {
        ssize_t r = read (c->fd, c->buf + c->got, REQUEST_MAX - c->got);

        if (r < 0) {
            if (errno == EINTR)
                continue;
            if (errno != EAGAIN && errno != EWOULDBLOCK)
                drop_client (c);
            return;
        }
        if (r == 0) {
            finish_client (c);
            return;
        }
        c->got += r;
        if (c->got == REQUEST_MAX || count_nuls (c->buf, c->got) >= 3) {
            finish_client (c);
            return;
        }
    }
}

/* Takes one waiting connection, in a free slot or in place of the oldest unfinished one */
static void
accept_client (int listen_fd)
{
    struct ucred cred;
    socklen_t len = sizeof cred;
    Client *slot = NULL;
    int fd;

    fd = accept4 (listen_fd, NULL, NULL, SOCK_CLOEXEC | SOCK_NONBLOCK);
    if (fd < 0)
        return;
    if (getsockopt (fd, SOL_SOCKET, SO_PEERCRED, &cred, &len) < 0) {
        answer (fd, "ERROR\n");
        close (fd);
        return;
    }

    for (int i = 0; i < MAX_CLIENTS; i++) {
        if (clients[i].fd < 0) {
            slot = &clients[i];
            break;
        }
        if (!slot || clients[i].deadline_ms < slot->deadline_ms)
            slot = &clients[i];
    }
    if (slot->fd >= 0) {
        syslog (LOG_WARNING, "%d connections without a request, dropping the oldest (uid %u)",
                MAX_CLIENTS, (unsigned) slot->cred.uid);
        drop_client (slot);
    }

    slot->fd = fd;
    slot->cred = cred;
    slot->got = 0;
    slot->deadline_ms = now_ms () + READ_TIMEOUT * 1000LL;
    /* A client writes its request right after connecting: mostly it is complete already */
    read_client (slot);
}

/* A client that did not finish in time: checked if the request is whole, else dropped */
static void
expire_client (Client *c)
{
    if (count_nuls (c->buf, c->got) >= 2)
        finish_client (c);
    else
        drop_client (c);
}

/* A listening socket of its own (tests) */
static int
listen_on (const char *path)
{
    struct sockaddr_un addr = { .sun_family = AF_UNIX };
    int fd;

    if (strlen (path) >= sizeof addr.sun_path)
        return -1;
    strcpy (addr.sun_path, path);
    fd = socket (AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0)
        return -1;
    unlink (path);
    if (bind (fd, (struct sockaddr *) &addr, sizeof addr) < 0 || chmod (path, 0666) < 0 || listen (fd, 8) < 0) {
        close (fd);
        return -1;
    }
    return fd;
}

int
main (int argc, char **argv)
{
    const char *listen_path = NULL;
    const char *pid, *fds;
    int listen_fd;

    for (int i = 1; i < argc; i++) {
        if (strcmp (argv[i], "--listen") == 0 && i + 1 < argc)
            listen_path = argv[++i];
        else if (strcmp (argv[i], "--state") == 0 && i + 1 < argc)
            state_dir = argv[++i];
        else if (strcmp (argv[i], "--enabled") == 0 && i + 1 < argc)
            enabled_file = argv[++i];
        else if (strcmp (argv[i], "--boot-id") == 0 && i + 1 < argc)
            snprintf (boot_id, sizeof boot_id, "%s", argv[++i]);
        else {
            fprintf (stderr, "Usage: plinius-pinguard [--listen PATH] [--state DIR] [--enabled FILE] [--boot-id ID]\n");
            return 2;
        }
    }

    openlog ("plinius-pinguard", LOG_PID, LOG_AUTHPRIV);
    signal (SIGPIPE, SIG_IGN);
    umask (077);
    if (!boot_id[0])
        read_boot_id ();

    if (listen_path) {
        listen_fd = listen_on (listen_path);
        if (listen_fd < 0) {
            fprintf (stderr, "plinius-pinguard: cannot listen on %s: %s\n", listen_path, strerror (errno));
            return 1;
        }
    } else {
        /* systemd socket activation: exactly one socket, fd 3 */
        pid = getenv ("LISTEN_PID");
        fds = getenv ("LISTEN_FDS");
        if (!pid || !fds || atol (pid) != getpid () || atoi (fds) != 1) {
            fprintf (stderr, "plinius-pinguard: start through plinius-pinguard.socket or with --listen\n");
            return 1;
        }
        listen_fd = 3;
        fcntl (listen_fd, F_SETFD, FD_CLOEXEC);
    }

    for (int i = 0; i < MAX_CLIENTS; i++)
        clients[i].fd = -1;

    for (;;) {
        struct pollfd pfds[MAX_CLIENTS + 1];
        Client *polled[MAX_CLIENTS + 1];
        long long now = now_ms (), next = -1;
        int n = 1, timeout, r;

        pfds[0] = (struct pollfd) { .fd = listen_fd, .events = POLLIN };
        polled[0] = NULL;
        for (int i = 0; i < MAX_CLIENTS; i++) {
            if (clients[i].fd < 0)
                continue;
            if (clients[i].deadline_ms <= now) {
                expire_client (&clients[i]);
                continue;
            }
            if (next < 0 || clients[i].deadline_ms < next)
                next = clients[i].deadline_ms;
            pfds[n] = (struct pollfd) { .fd = clients[i].fd, .events = POLLIN };
            polled[n++] = &clients[i];
        }

        /* Idle without any connection: exit, the socket stays with systemd */
        if (next >= 0)
            timeout = (int) (next - now);
        else
            timeout = listen_path ? -1 : IDLE_EXIT * 1000;

        r = poll (pfds, n, timeout);
        if (r == 0 && next < 0)
            return 0;
        if (r < 0) {
            if (errno == EINTR)
                continue;
            return 1;
        }

        for (int i = 1; i < n; i++)
            if (polled[i]->fd >= 0 && (pfds[i].revents & (POLLIN | POLLHUP | POLLERR)))
                read_client (polled[i]);
        if (pfds[0].revents & POLLIN)
            accept_client (listen_fd);
    }
}
