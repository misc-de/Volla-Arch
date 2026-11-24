"""Command line for testing the steps of the DOS Installer without its window.

    python3 -m dosinstall floppies --target DIR [--program NAME] IMAGE...
    python3 -m dosinstall finish --target DIR
    python3 -m dosinstall install PATH...      (floppy images, CD, ZIP, GOG installer, folder)
"""

import argparse
import sys
import time

from .dosbox import install_from_floppies
from .finish import finish
from .install import install
from .sources import classify


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def main():
    parser = argparse.ArgumentParser(prog="dosinstall")
    sub = parser.add_subparsers(dest="cmd", required=True)
    fl = sub.add_parser("floppies", help="run an installer from floppy images in DOSBox")
    fl.add_argument("--target", required=True, help="directory that becomes C:")
    fl.add_argument("--program", help="installer on disk 1 (found by itself if not given)")
    fl.add_argument("images", nargs="+")
    fi = sub.add_parser("finish", help="add what was installed to ScummVM, with launchers")
    fi.add_argument("--target", required=True)
    ins = sub.add_parser("install", help="the whole way, as the app does it")
    ins.add_argument("paths", nargs="+")
    args = parser.parse_args()
    if args.cmd == "install":
        game = install(classify(args.paths))
        log(f"installed: {game.title} ({game.kind}) in {game.folder}, targets {game.targets}")
        return 0
    if args.cmd == "floppies":
        code, _ = install_from_floppies(args.images, args.target, args.program, log=log)
        if code == 0:
            finish(args.target, log=log)
        return code
    if args.cmd == "finish":
        return 0 if finish(args.target, log=log) else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
