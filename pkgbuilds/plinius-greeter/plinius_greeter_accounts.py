"""The accounts one can log in with, the same rule for the greeter and for root.

plinius-greeter (as the user greeter) shows them, plinius-greeter-config (as root) counts them to
decide between logging in at once and the greeter, and only ever starts a session for one of
them. A login account has a user ID between UID_MIN and UID_MAX of /etc/login.defs and a shell
listed in /etc/shells. That leaves out root, the system users and the Android IDs of the phone
(device-volla-ansuz' sysusers, shell nologin), and it is what AccountsService and useradd call a
regular user.

SPDX-License-Identifier: MIT
"""

import pwd

LOGIN_DEFS = "/etc/login.defs"
SHELLS = "/etc/shells"


def _login_defs():
    values = {"UID_MIN": 1000, "UID_MAX": 60000}
    try:
        with open(LOGIN_DEFS, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0] in values:
                    try:
                        values[parts[0]] = int(parts[1])
                    except ValueError:
                        pass
    except OSError:
        pass
    return values["UID_MIN"], values["UID_MAX"]


def _shells():
    shells = set()
    try:
        with open(SHELLS, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    shells.add(line)
    except OSError:
        pass
    return {s for s in shells if not s.endswith(("/nologin", "/false"))}


def login_users():
    """The login accounts as pwd entries, by user ID."""
    uid_min, uid_max = _login_defs()
    shells = _shells()
    seen = set()
    users = []
    for entry in sorted(pwd.getpwall(), key=lambda e: e.pw_uid):
        if entry.pw_name in seen:
            continue
        if uid_min <= entry.pw_uid <= uid_max and entry.pw_shell in shells:
            seen.add(entry.pw_name)
            users.append(entry)
    return users


def is_login_user(name):
    return any(entry.pw_name == name for entry in login_users())


def real_name(entry):
    """The full name from the GECOS field, else the user name."""
    return entry.pw_gecos.split(",")[0].strip() or entry.pw_name
