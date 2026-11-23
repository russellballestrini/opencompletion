#!/usr/bin/env python3
"""Uninstall what our lock files no longer name: `pip install -r` adds &
upgrades but never removes, so a dependency we dropped (together, pandas
under openai[datalib], 2026-11-21) stayed in every venv built before, our
demo's included. `make venv` runs this after installing.

python venv_prune.py LOCK [LOCK ...]   (run with the venv's python)
Prints what it removes; removes nothing when every lock names nothing.
"""

import re
import subprocess
import sys
from importlib import metadata

# pip's own tools are never ours to remove.
KEEP = {"pip", "setuptools", "wheel"}
PINNED = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==")


def canonical(name):
    """A distribution name as PEP 503 compares them."""
    return re.sub(r"[-_.]+", "-", name).lower()


def locked(paths):
    names = set()
    for path in paths:
        with open(path) as lock:
            for line in lock:
                match = PINNED.match(line)
                if match:
                    names.add(canonical(match.group(1)))
    return names


def installed():
    return {
        canonical(dist.metadata["Name"])
        for dist in metadata.distributions()
        if dist.metadata["Name"]
    }


def strays(locked_names, installed_names):
    return sorted(installed_names - locked_names - KEEP)


def main(paths):
    names = locked(paths)
    if not names:
        print("venv_prune: no pinned packages read; nothing removed")
        return 0
    extra = strays(names, installed())
    if not extra:
        return 0
    print("venv_prune: removing what no lock names:", " ".join(extra))
    return subprocess.call([sys.executable, "-m", "pip", "uninstall", "-y", *extra])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
