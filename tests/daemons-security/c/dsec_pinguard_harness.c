/*
 * Harness for plinius-pinguard.c: the daemon's request handling with fakes for the clock
 * (CLOCK_BOOTTIME), /etc/shadow, the user database and syslog, so waits of minutes and hours
 * are checked without waiting and right passwords without root. State and marker in a test
 * directory.
 *
 * Commands on stdin, one per line; each prints one line:
 *   dirs STATE MARKER        state directory and the "wait is on" marker
 *   bootid ID                this boot
 *   nobootid                 no boot id could be read
 *   clock SECONDS            CLOCK_BOOTTIME as the daemon sees it
 *   hash USER PASSWORD       the user's shadow entry becomes a hash of PASSWORD
 *   shadow USER ENTRY        the raw shadow entry ("!", "*", "-" for none)
 *   lock USER                "!" before the user's hash, as passwd -l does
 *   req UID HEX              handle_request() with peer uid UID and the request bytes HEX;
 *                            prints the answer without its newline, "<none>" for none
 *   wait N                   pinguard_wait_after(N)
 *   format N                 pinguard_format_wait(N)
 *   unreachable ERRNO        pinguard_unreachable(ERRNO)
 *   ask PATH                 pinguard_ask() on PATH for "alice"/"x": "ok ANSWER" or "err ERRNO"
 *   asklong PATH             pinguard_ask() with a request over REQUEST_MAX: "err ERRNO"
 *   asknosock PATH           pinguard_ask() when no socket can be made: "err ERRNO"
 *   asknomem PATH            pinguard_ask() when its request finds no memory: "err ERRNO"
 *   oom                      the next calloc() of the daemon fails (password_matches)
 * Fake users: root 0, alice 1001, bob 1002.
 *
 * Daemon mode, with any argument: the daemon's own main() with those arguments, on the real
 * clock. The fake user database then also holds the caller ("self", its uid and real name),
 * with a hash of DSEC_SELF_PIN or without one (as the daemon sees it without root). The kernel
 * calls of the main loop can fail on purpose, DSEC_FAULTS="KIND=N,...": the Nth call of that
 * kind fails (accept: accept4 EAGAIN, peercred: SO_PEERCRED, eintr/eio: the Nth read, poll:
 * EINVAL, socket: EMFILE). /proc/sys/kernel/random/boot_id is read from DSEC_BOOT_ID_FILE
 * (unset: there is none). SIGTERM ends the daemon through exit(0) in its next poll, so the
 * counters of a coverage build are written; DSEC_IDLE_MS shortens its 60 s idle exit.
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

#include "dsec_nolog.h"

static long long fake_boot_seconds = 1000;

static int
fake_clock_gettime (clockid_t id, struct timespec *ts)
{
    if (id == CLOCK_BOOTTIME && fake_boot_seconds >= 0) {
        ts->tv_sec = fake_boot_seconds;
        ts->tv_nsec = 0;
        return 0;
    }
    return clock_gettime (id, ts);
}

static struct {
    const char *name;
    uid_t uid;
    char shadow[256];
    bool has_shadow;
} users[] = {
    { "root", 0, "", false },
    { "alice", 1001, "", false },
    { "bob", 1002, "", false },
    { "", 0, "", false },       /* daemon mode: the caller */
};

static struct passwd fake_pw;
static struct spwd fake_sp;

static struct passwd *
fake_getpwnam (const char *name)
{
    for (size_t i = 0; i < sizeof users / sizeof users[0]; i++)
        if (users[i].name[0] && strcmp (users[i].name, name) == 0) {
            memset (&fake_pw, 0, sizeof fake_pw);
            fake_pw.pw_name = (char *) users[i].name;
            fake_pw.pw_uid = users[i].uid;
            fake_pw.pw_gid = users[i].uid;
            fake_pw.pw_dir = (char *) "/nonexistent";
            return &fake_pw;
        }
    return NULL;
}

static struct spwd *
fake_getspnam (const char *name)
{
    for (size_t i = 0; i < sizeof users / sizeof users[0]; i++)
        if (users[i].name[0] && strcmp (users[i].name, name) == 0 && users[i].has_shadow) {
            memset (&fake_sp, 0, sizeof fake_sp);
            fake_sp.sp_namp = (char *) users[i].name;
            fake_sp.sp_pwdp = users[i].shadow;
            return &fake_sp;
        }
    return NULL;
}

/* The daemon's idle exit (the same definition as there), shortened by DSEC_IDLE_MS */
#define IDLE_EXIT 60

/* ---- Daemon mode: faults of the kernel calls the main loop makes ---- */

static bool daemon_mode;
static volatile sig_atomic_t stop_requested;
static int fail_next_calloc;

/* True if DSEC_FAULTS names call number @count (1-based) for @kind */
static bool
fault_at (int count, const char *kind)
{
    const char *spec = getenv ("DSEC_FAULTS");
    size_t len = strlen (kind);

    if (!daemon_mode || !spec)
        return false;
    for (const char *p = spec; (p = strstr (p, kind)); p += len)
        if ((p == spec || p[-1] == ',') && p[len] == '=' && atoi (p + len + 1) == count)
            return true;
    return false;
}

static int n_accept, n_peercred, n_read, n_poll, n_socket;

static int
fake_accept4 (int fd, struct sockaddr *addr, socklen_t *len, int flags)
{
    if (fault_at (++n_accept, "accept")) {
        errno = EAGAIN;
        return -1;
    }
    return accept4 (fd, addr, len, flags);
}

static int
fake_getsockopt (int fd, int level, int name, void *value, socklen_t *len)
{
    if (name == SO_PEERCRED && fault_at (++n_peercred, "peercred")) {
        errno = ENOTCONN;
        return -1;
    }
    return getsockopt (fd, level, name, value, len);
}

static ssize_t
fake_read (int fd, void *buf, size_t size)
{
    n_read++;
    if (fault_at (n_read, "eintr")) {
        errno = EINTR;
        return -1;
    }
    if (fault_at (n_read, "eio")) {
        errno = EIO;
        return -1;
    }
    return read (fd, buf, size);
}

static int
fake_poll (struct pollfd *fds, nfds_t n, int timeout)
{
    struct timespec ts;
    sigset_t open_mask;
    const char *idle = getenv ("DSEC_IDLE_MS");
    int r;

    if (fault_at (++n_poll, "poll")) {
        errno = EINVAL;
        return -1;
    }
    if (stop_requested)
        exit (0);
    if (timeout == IDLE_EXIT * 1000 && idle)
        timeout = atoi (idle);
    ts.tv_sec = timeout / 1000;
    ts.tv_nsec = (timeout % 1000) * 1000000L;
    /* SIGTERM and SIGUSR1 are blocked outside of here: no signal slips in before the wait */
    sigemptyset (&open_mask);
    r = ppoll (fds, n, timeout < 0 ? NULL : &ts, &open_mask);
    if (stop_requested)
        exit (0);
    return r;
}

static int
fake_socket (int domain, int type, int protocol)
{
    if (fault_at (++n_socket, "socket")) {
        errno = EMFILE;
        return -1;
    }
    return socket (domain, type, protocol);
}

static FILE *
fake_fopen (const char *path, const char *mode)
{
    if (strcmp (path, "/proc/sys/kernel/random/boot_id") == 0) {
        const char *file = getenv ("DSEC_BOOT_ID_FILE");

        if (!file) {
            errno = ENOENT;
            return NULL;
        }
        return fopen (file, mode);
    }
    return fopen (path, mode);
}

static void *
fake_calloc (size_t n, size_t size)
{
    if (fail_next_calloc) {
        fail_next_calloc = 0;
        errno = ENOMEM;
        return NULL;
    }
    return calloc (n, size);
}

#define clock_gettime(id, ts) fake_clock_gettime (id, ts)
#define accept4 fake_accept4
#define getsockopt fake_getsockopt
#define read fake_read
#define poll fake_poll
#define socket fake_socket
#define fopen fake_fopen
#define calloc fake_calloc
#define getpwnam(name) fake_getpwnam (name)
#define getspnam(name) fake_getspnam (name)
#define main pinguard_main
#include "plinius-pinguard/plinius-pinguard.c"
#undef main
#undef clock_gettime
#undef accept4
#undef getsockopt
#undef read
#undef poll
#undef socket
#undef fopen
#undef calloc

/* For the client: a socket() or malloc() that fails once when asked */
static int fail_client_socket, fail_client_malloc;

static int
client_socket (int domain, int type, int protocol)
{
    if (fail_client_socket) {
        fail_client_socket = 0;
        errno = EMFILE;
        return -1;
    }
    return socket (domain, type, protocol);
}

static void *
client_malloc (size_t size)
{
    if (fail_client_malloc) {
        fail_client_malloc = 0;
        return NULL;
    }
    return malloc (size);
}

#define socket client_socket
#define malloc client_malloc

#include "plinius-pinguard/plinius-pinguard-client.h"
#undef socket
#undef malloc

static int
unhex (const char *hex, char *out, size_t size)
{
    size_t n = 0;

    for (; hex[0] && hex[1] && n < size; hex += 2) {
        unsigned v;

        if (sscanf (hex, "%2x", &v) != 1)
            return -1;
        out[n++] = (char) v;
    }
    return (int) n;
}

static void
do_request (unsigned uid, const char *hex)
{
    char buf[REQUEST_MAX + 1], answer_buf[128];
    struct ucred cred = { .pid = 1, .uid = uid, .gid = uid };
    int sv[2], len;
    ssize_t n;

    len = unhex (hex, buf, REQUEST_MAX);
    if (len < 0 || socketpair (AF_UNIX, SOCK_STREAM, 0, sv) < 0) {
        puts ("<harness error>");
        return;
    }
    buf[len] = '\0';
    handle_request (sv[0], cred, buf, (size_t) len);
    close (sv[0]);
    n = read (sv[1], answer_buf, sizeof answer_buf - 1);
    close (sv[1]);
    if (n <= 0) {
        puts ("<none>");
        return;
    }
    answer_buf[n] = '\0';
    answer_buf[strcspn (answer_buf, "\n")] = '\0';
    puts (answer_buf);
}

static void
on_stop (int sig)
{
    (void) sig;
    stop_requested = 1;
}

static void
on_wake (int sig)
{
    (void) sig;
}

/* The daemon's main() as plinius-pinguard ARGS..., see the top */
static int
run_daemon (int argc, char **argv)
{
    static char self_name[64];
    struct passwd *me = getpwuid (getuid ());
    struct sigaction stop = { .sa_handler = on_stop }, wake = { .sa_handler = on_wake };
    const char *pin = getenv ("DSEC_SELF_PIN");
    sigset_t blocked;
    size_t self = sizeof users / sizeof users[0] - 1;

    daemon_mode = true;
    fake_boot_seconds = -1;
    snprintf (self_name, sizeof self_name, "%s", me ? me->pw_name : "self");
    users[self].name = self_name;
    users[self].uid = getuid ();
    if (pin) {
        struct crypt_data data = { 0 };
        const char *h = crypt_r (pin, "$6$dsectestsalt$", &data);

        snprintf (users[self].shadow, sizeof users[self].shadow, "%s", h ? h : "*");
        users[self].has_shadow = true;
    }
    /* No SA_RESTART: a signal ends the wait in poll */
    sigaction (SIGTERM, &stop, NULL);
    sigaction (SIGUSR1, &wake, NULL);
    sigemptyset (&blocked);
    sigaddset (&blocked, SIGTERM);
    sigaddset (&blocked, SIGUSR1);
    sigprocmask (SIG_BLOCK, &blocked, NULL);
    return pinguard_main (argc, argv);
}

int
main (int argc, char **argv)
{
    char line[8192];

    if (argc > 1)
        return run_daemon (argc, argv);
    setvbuf (stdout, NULL, _IOLBF, 0);
    while (fgets (line, sizeof line, stdin)) {
        char a[4200] = "", b[4200] = "";
        long long n;

        line[strcspn (line, "\n")] = '\0';
        if (sscanf (line, "dirs %4199s %4199s", a, b) == 2) {
            state_dir = strdup (a);
            enabled_file = strdup (b);
            puts ("ok");
        } else if (sscanf (line, "bootid %36s", a) == 1) {
            snprintf (boot_id, sizeof boot_id, "%s", a);
            puts ("ok");
        } else if (sscanf (line, "clock %lld", &n) == 1) {
            fake_boot_seconds = n;
            puts ("ok");
        } else if (sscanf (line, "hash %63s %255s", a, b) == 2 ||
                   sscanf (line, "shadow %63s %255s", a, b) == 2) {
            bool raw = strncmp (line, "shadow", 6) == 0;

            for (size_t i = 0; i < sizeof users / sizeof users[0]; i++) {
                if (strcmp (users[i].name, a) != 0)
                    continue;
                if (raw && strcmp (b, "-") == 0) {
                    users[i].has_shadow = false;
                } else if (raw) {
                    snprintf (users[i].shadow, sizeof users[i].shadow, "%s", b);
                    users[i].has_shadow = true;
                } else {
                    /* SHA-512 with a fixed salt: fast, and libcrypt checks it like yescrypt */
                    struct crypt_data data = { 0 };
                    const char *h = crypt_r (b, "$6$dsectestsalt$", &data);

                    snprintf (users[i].shadow, sizeof users[i].shadow, "%s", h ? h : "*");
                    users[i].has_shadow = true;
                }
            }
            puts ("ok");
        } else if (sscanf (line, "lock %63s", a) == 1) {
            for (size_t i = 0; i < sizeof users / sizeof users[0]; i++)
                if (strcmp (users[i].name, a) == 0) {
                    char locked[256];

                    snprintf (locked, sizeof locked, "!%s", users[i].shadow);
                    memcpy (users[i].shadow, locked, sizeof locked);
                }
            puts ("ok");
        } else if (sscanf (line, "req %lld %4199s", &n, a) >= 1 && strncmp (line, "req", 3) == 0) {
            do_request ((unsigned) n, a);
        } else if (sscanf (line, "wait %lld", &n) == 1) {
            printf ("%ld\n", pinguard_wait_after ((unsigned) n));
        } else if (sscanf (line, "format %lld", &n) == 1) {
            char text[64];

            pinguard_format_wait ((long) n, text, sizeof text);
            puts (text);
        } else if (sscanf (line, "unreachable %lld", &n) == 1) {
            puts (pinguard_unreachable ((int) n) ? "yes" : "no");
        } else if (sscanf (line, "asklong %4199s", a) == 1) {
            static char big[REQUEST_MAX + 8];
            char answer_text[64];

            memset (big, 'p', sizeof big - 1);
            if (pinguard_ask (a, "alice", big, NULL, answer_text, sizeof answer_text) < 0)
                printf ("err %d\n", errno);
            else
                printf ("ok %s\n", answer_text);
        } else if (sscanf (line, "asknosock %4199s", a) == 1 || sscanf (line, "asknomem %4199s", a) == 1) {
            char answer_text[64];

            if (strncmp (line, "asknosock", 9) == 0)
                fail_client_socket = 1;
            else
                fail_client_malloc = 1;
            if (pinguard_ask (a, "alice", "x", NULL, answer_text, sizeof answer_text) < 0)
                printf ("err %d\n", errno);
            else
                printf ("ok %s\n", answer_text);
        } else if (strcmp (line, "nobootid") == 0) {
            boot_id[0] = '\0';
            puts ("ok");
        } else if (strcmp (line, "oom") == 0) {
            fail_next_calloc = 1;
            puts ("ok");
        } else if (sscanf (line, "ask %4199s", a) == 1) {
            char answer_text[64];

            if (pinguard_ask (a, "alice", "x", NULL, answer_text, sizeof answer_text) < 0)
                printf ("err %d\n", errno);
            else
                printf ("ok %s\n", answer_text);
        } else {
            printf ("<unknown command: %s>\n", line);
        }
    }
    return 0;
}
