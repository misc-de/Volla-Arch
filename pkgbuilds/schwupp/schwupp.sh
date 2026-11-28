#!/bin/sh
# Schwupp lives in /usr/lib/schwupp with the libraries Arch does not package
# (pyatv, pywebostv and theirs) in vendor/; its package is called "app", too
# generic for site-packages.
export PYTHONPATH="/usr/lib/schwupp:/usr/lib/schwupp/vendor${PYTHONPATH:+:$PYTHONPATH}"
# Unbuffered: as a D-Bus service its output goes to the journal, buffered it arrived only at exit
export PYTHONUNBUFFERED=1
# -P: the current directory is not searched, an "app" folder there would win
exec /usr/bin/python3 -P -m app "$@"
