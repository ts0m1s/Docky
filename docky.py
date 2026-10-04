#!/usr/bin/env python3
# docky.py -- the `docky` command. The code lives in the docky/ package
# next to this file; this launcher only starts it.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

try:
    from docky.cli import main
except ImportError as error:
    # The docky/ folder is missing or incomplete -- e.g. an old updater that
    # only copied top-level files. Say how to fix it instead of a traceback.
    sys.stderr.write(
        f"! Docky's files are incomplete ({error}).\n"
        "  Reinstall to repair it:\n"
        "    curl -fsSL https://raw.githubusercontent.com/ts0m1s/Docky/main/install.sh | sh\n"
    )
    sys.exit(1)

if __name__ == "__main__":
    main()
