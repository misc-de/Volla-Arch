/*
 * test-pinguard: runs plinius-pinguard on a socket, state directory and marker of its own and
 * checks what it answers (see plinius-pinguard.h). Only for the calling user, whose password the
 * daemon cannot read without root: every password check is a wrong one here, which is what the
 * counting needs. Nothing outside the test directory is touched.
 *
 *   test-pinguard DAEMON DIR    DIR: an empty directory of the test
 *
 * SPDX-License-Identifier: MIT
 */

#define _GNU_SOURCE
#include <fcntl.h>
#include <poll.h>
#include <pwd.h>
#include <signal.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>

#include "plinius-pinguard-client.h"

static const char *daemon_path, *dir;
static char sock[256], state[256], marker[256], user[64];
static pid_t daemon_pid;
static int failed;

#define CHECK(cond, ...)                                                                    \
    do {                                                                                    \
        if (!(cond)) {                                                                      \
            fprintf (stderr, "FAIL %s:%d: ", __FILE__, __LINE__);                           \
            fprintf (stderr, __VA_ARGS__);                                                  \
            fputc ('\n', stderr);                                                           \
            failed++;                                                                       \
        }                                                                                   \
    } while (0)

static void
stop_daemon (void)
{
    if (daemon_pid > 0) {
        kill (daemon_pid, SIGTERM);
        waitpid (daemon_pid, NULL, 0);
        daemon_pid = 0;
    }
}

/* A fresh daemon as if in boot @boot */
static void
start_daemon (const char *boot)
{
    stop_daemon ();
    unlink (sock);
    daemon_pid = fork ();
    if (daemon_pid == 0) {
        execl (daemon_path, daemon_path, "--listen", sock, "--state", state, "--enabled", marker,
               "--boot-id", boot, (char *) NULL);
        _exit (127);
    }
    for (int i = 0; i < 100 && access (sock, F_OK) < 0; i++)
        usleep (20000);
}

static void
set_wait (bool on)
{
    if (on)
        close (open (marker, O_WRONLY | O_CREAT | O_CLOEXEC, 0600));
    else
        unlink (marker);
}

static void
clear_state (void)
{
    char path[512];

    snprintf (path, sizeof path, "%s/%u", state, (unsigned) getuid ());
    unlink (path);
    snprintf (path, sizeof path, "%s/%u.network", state, (unsigned) getuid ());
    unlink (path);
}

static void
write_state (const char *content)
{
    char path[512];
    FILE *f;

    snprintf (path, sizeof path, "%s/%u", state, (unsigned) getuid ());
    f = fopen (path, "we");
    fputs (content, f);
    fclose (f);
}

/* The daemon's answer; "" if there was none */
static const char *
ask (const char *password, const char *kind)
{
    static char answer[64];

    if (pinguard_ask (sock, user, password, kind, answer, sizeof answer) < 0)
        answer[0] = '\0';
    return answer;
}

static void
expect (const char *password, const char *kind, const char *want)
{
    const char *got = ask (password, kind);

    CHECK (strcmp (got, want) == 0, "%s/%s: got \"%s\", want \"%s\"", password, kind ? kind : "-",
           got, want);
}

static void
test_wait_off (void)
{
    set_wait (false);
    clear_state ();
    start_daemon ("boot-a");
    for (int i = 0; i < 8; i++)
        expect ("wrong", NULL, "FAIL 0 0");
    expect ("", PINGUARD_ATTEMPT, "COUNTED 0 0");
    expect ("", PINGUARD_SUCCESS, "OK");
}

static void
test_pin_waits (void)
{
    char want[32];

    set_wait (true);
    clear_state ();
    start_daemon ("boot-a");
    for (unsigned i = 1; i <= FREE_TRIES; i++) {
        snprintf (want, sizeof want, "FAIL %u 0", i);
        expect ("wrong", NULL, want);
    }
    expect ("wrong", NULL, "FAIL 6 30");
    CHECK (strncmp (ask ("wrong", NULL), "WAIT ", 5) == 0, "no wait after 6 wrong PINs");
    /* The network has a counter of its own */
    expect ("wrong", PINGUARD_NETWORK, "FAIL 1 0");
}

static void
test_pattern_counts (void)
{
    char want[32];

    set_wait (true);
    clear_state ();
    start_daemon ("boot-a");
    for (unsigned i = 1; i <= 3; i++) {
        snprintf (want, sizeof want, "COUNTED %u 0", i);
        expect ("", PINGUARD_ATTEMPT, want);
    }
    /* A right pattern clears what was counted */
    expect ("", PINGUARD_SUCCESS, "OK");
    expect ("", PINGUARD_ATTEMPT, "COUNTED 1 0");
    /* Pattern and PIN share the count */
    for (unsigned i = 2; i <= FREE_TRIES; i++) {
        snprintf (want, sizeof want, "FAIL %u 0", i);
        expect ("wrong", NULL, want);
    }
    expect ("", PINGUARD_ATTEMPT, "COUNTED 6 30");
    CHECK (strncmp (ask ("", PINGUARD_ATTEMPT), "WAIT ", 5) == 0, "a pattern try during the wait");
    CHECK (strncmp (ask ("wrong", NULL), "WAIT ", 5) == 0, "a PIN try during the wait");
}

static void
test_malformed (void)
{
    set_wait (true);
    clear_state ();
    start_daemon ("boot-a");
    expect ("secret", PINGUARD_ATTEMPT, "ERROR");
    expect ("secret", PINGUARD_SUCCESS, "ERROR");
    expect ("wrong", "other", "ERROR");
    if (getuid () != 0) {
        char answer[64] = ""; /* printed also when pinguard_ask () failed and wrote nothing */

        CHECK (pinguard_ask (sock, "root", "x", NULL, answer, sizeof answer) == 0 &&
               strcmp (answer, "DENIED") == 0, "root's password for anyone: \"%s\"", answer);
    }
}

/*
 * Tries from another account ("from:UID"): only root may say so (setuid su and polkit's helper);
 * from anyone else the daemon refuses, so nobody files its tries under a counter of its choice.
 * Malformed uids and uid 0 are errors. The counting itself needs root (it checks /etc/shadow)
 * and is tested on the phone (see the package's test notes).
 */
static void
test_from_other_account (void)
{
    set_wait (false);
    clear_state ();
    start_daemon ("boot-a");
    if (getuid () != 0) {
        expect ("wrong", PINGUARD_FROM "12345", "DENIED");
        /* Not even for the caller itself */
        {
            char kind[32];

            snprintf (kind, sizeof kind, "%s%u", PINGUARD_FROM, (unsigned) getuid ());
            expect ("wrong", kind, "DENIED");
        }
    }
    expect ("wrong", PINGUARD_FROM "0", "ERROR");
    expect ("wrong", PINGUARD_FROM "012", "ERROR");
    expect ("wrong", PINGUARD_FROM "-1", "ERROR");
    expect ("wrong", PINGUARD_FROM "12x", "ERROR");
    expect ("wrong", PINGUARD_FROM "99999999999", "ERROR");
    expect ("wrong", PINGUARD_FROM, "ERROR");
}

/* A wait from before a restart starts over in full; the clock plays no part */
static void
test_restart (void)
{
    char path[512], line[128] = "";
    FILE *f;

    set_wait (true);
    clear_state ();
    /* Over long ago by the clock, in the old format */
    write_state ("6 1\n");
    start_daemon ("boot-b");
    expect ("wrong", NULL, "WAIT 30");

    /* Written back for this boot: the next try does not start it again */
    snprintf (path, sizeof path, "%s/%u", state, (unsigned) getuid ());
    f = fopen (path, "re");
    if (f) {
        if (!fgets (line, sizeof line, f))
            line[0] = '\0';
        fclose (f);
    }
    CHECK (strncmp (line, "2 6 boot-b ", 11) == 0, "state after the restart: \"%s\"", line);
    sleep (1);
    CHECK (strcmp (ask ("wrong", NULL), "WAIT 30") != 0, "the wait started over again");

    /* Another boot in the new format, with a deadline long over */
    write_state ("2 7 boot-a 0\n");
    start_daemon ("boot-c");
    expect ("wrong", NULL, "WAIT 60");
}

/* A client that sends nothing holds the daemon up for READ_TIMEOUT at most */
static void
test_slow_client (void)
{
    struct sockaddr_un addr = { .sun_family = AF_UNIX };
    struct timespec start, end;
    double seconds;
    int fd;

    set_wait (false);
    clear_state ();
    start_daemon ("boot-a");
    strcpy (addr.sun_path, sock);
    fd = socket (AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (connect (fd, (struct sockaddr *) &addr, sizeof addr) < 0) {
        CHECK (false, "connect: %m");
        return;
    }
    /* One byte a second would keep a per-read timeout from ever running out */
    if (fork () == 0) {
        for (int i = 0; i < 12; i++) {
            if (write (fd, "x", 1) != 1)
                break;
            sleep (1);
        }
        _exit (0);
    }
    usleep (200000);
    clock_gettime (CLOCK_MONOTONIC, &start);
    expect ("wrong", NULL, "FAIL 0 0");
    clock_gettime (CLOCK_MONOTONIC, &end);
    seconds = (end.tv_sec - start.tv_sec) + (end.tv_nsec - start.tv_nsec) / 1e9;
    CHECK (seconds < 7, "the next client waited %.1f s", seconds);
    close (fd);
    wait (NULL);
}

/*
 * Many connections that send nothing, more than the daemon reads at once: the next real request
 * is still answered at once, not after their READ_TIMEOUT each (before 0.6 seven of them made the
 * lock screen's request time out, and the PAM module fell back to pam_unix without the count).
 */
static void
test_silent_clients (void)
{
    struct sockaddr_un addr = { .sun_family = AF_UNIX };
    struct timespec start, end;
    int fds[40], dropped = 0;
    double seconds;

    set_wait (true);
    clear_state ();
    start_daemon ("boot-a");
    strcpy (addr.sun_path, sock);
    for (int i = 0; i < 40; i++) {
        fds[i] = socket (AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
        if (connect (fds[i], (struct sockaddr *) &addr, sizeof addr) < 0) {
            CHECK (false, "connect %d: %m", i);
            fds[i] = -1;
        }
    }
    usleep (200000);
    clock_gettime (CLOCK_MONOTONIC, &start);
    expect ("wrong", NULL, "FAIL 1 0");
    clock_gettime (CLOCK_MONOTONIC, &end);
    seconds = (end.tv_sec - start.tv_sec) + (end.tv_nsec - start.tv_nsec) / 1e9;
    CHECK (seconds < 2, "with 40 silent connections the answer took %.1f s", seconds);

    /* The oldest silent ones made room: they see the end of the stream */
    for (int i = 0; i < 40; i++) {
        struct pollfd pfd = { .fd = fds[i], .events = POLLIN };
        char c;

        if (fds[i] >= 0 && poll (&pfd, 1, 0) == 1 && read (fds[i], &c, 1) == 0)
            dropped++;
    }
    CHECK (dropped >= 8, "only %d of 40 silent connections were dropped", dropped);
    for (int i = 0; i < 40; i++)
        if (fds[i] >= 0)
            close (fds[i]);
    set_wait (false);
    clear_state ();
}

/* Only a missing daemon may fall back; one that does not answer must not */
static void
test_unreachable (void)
{
    char answer[64];

    CHECK (pinguard_ask ("/nonexistent/pinguard.socket", user, "x", NULL, answer, sizeof answer) < 0 &&
           pinguard_unreachable (errno), "missing socket not seen as unreachable");
    CHECK (!pinguard_unreachable (ETIMEDOUT), "a timeout counted as unreachable");
    CHECK (!pinguard_unreachable (EINVAL), "an overlong request counted as unreachable");
}

int
main (int argc, char **argv)
{
    struct passwd *pw = getpwuid (getuid ());

    if (argc != 3 || !pw) {
        fprintf (stderr, "Usage: test-pinguard DAEMON DIR\n");
        return 2;
    }
    daemon_path = argv[1];
    dir = argv[2];
    snprintf (user, sizeof user, "%s", pw->pw_name);
    snprintf (sock, sizeof sock, "%s/socket", dir);
    snprintf (state, sizeof state, "%s/state", dir);
    snprintf (marker, sizeof marker, "%s/enabled", dir);
    mkdir (state, 0700);
    signal (SIGPIPE, SIG_IGN);

    test_wait_off ();
    test_pin_waits ();
    test_pattern_counts ();
    test_malformed ();
    test_from_other_account ();
    test_restart ();
    test_slow_client ();
    test_silent_clients ();
    test_unreachable ();
    stop_daemon ();

    if (failed)
        fprintf (stderr, "%d check(s) failed\n", failed);
    else
        printf ("test-pinguard: all checks passed\n");
    return failed ? 1 : 0;
}
