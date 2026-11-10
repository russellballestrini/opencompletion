"""asyncio's executor under gevent's monkey patch (gevent_asyncio.py): the
second asyncio.to_thread on a loop reuses an idle worker & must return. On
CPython 3.13.12+, 3.14.3+ & 3.15 it hung without our wake-up; each case
runs in its own interpreter, since a patch can't be taken back."""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

SCRIPT = textwrap.dedent("""
    from gevent import monkey

    monkey.patch_all()
    {shim}
    import asyncio, os, sys


    def watchdog():  # a real thread: patched ones can't run while we hang
        monkey.get_original("time", "sleep")(8)
        sys.__stdout__.write("HUNG\\n")
        sys.__stdout__.flush()
        os._exit(3)


    monkey.get_original("_thread", "start_new_thread")(watchdog, ())


    async def main():
        loop = asyncio.get_running_loop()
        first = await asyncio.to_thread(int, "1")  # starts a worker
        await asyncio.sleep(0.05)  # it goes idle
        second = await asyncio.to_thread(int, "2")  # reused
        third = await loop.run_in_executor(None, int, "3")
        return first + second + third


    print("sum", asyncio.run(main()))
    """)


def run(shim):
    return subprocess.run(
        [sys.executable, "-c", SCRIPT.format(shim=shim)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_a_reused_executor_worker_still_wakes_its_loop():
    done = run("import gevent_asyncio; gevent_asyncio.wake_executor_jobs()")
    assert done.returncode == 0 and done.stdout.strip() == "sum 6", (
        done.stdout + done.stderr
    )


def test_asking_twice_patches_once():
    done = run(
        "import gevent_asyncio; gevent_asyncio.wake_executor_jobs(); "
        "first = __import__('asyncio').base_events.BaseEventLoop.run_in_executor; "
        "gevent_asyncio.wake_executor_jobs(); "
        "assert __import__('asyncio').base_events.BaseEventLoop.run_in_executor is first"
    )
    assert done.returncode == 0 and done.stdout.strip() == "sum 6", (
        done.stdout + done.stderr
    )


def test_our_app_applies_it_where_it_patches():
    source = (ROOT / "app.py").read_text()
    patched = source.index("monkey.patch_all()")
    assert "gevent_asyncio.wake_executor_jobs()" in source[patched : patched + 400]


@pytest.mark.skipif(
    sys.version_info < (3, 13, 12) or (3, 14) <= sys.version_info < (3, 14, 3),
    reason="this CPython's asyncio still wakes its loop from an executor job",
)
def test_without_it_this_python_hangs():
    """Why the module exists: when this starts failing, gevent or CPython
    fixed it & gevent_asyncio.py can go."""
    assert run("").returncode == 3
