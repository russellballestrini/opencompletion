"""asyncio.to_thread under gevent's monkey patch never returns once an
executor worker is reused.

Hangs on CPython 3.13.12+, 3.14.3+ & 3.15; passes on 3.12, 3.13.0-3.13.11 &
3.14.0-3.14.2. Needs only gevent (26.9.0 tested).

    python gevent_asyncio_executor_repro.py   # exit 0 = OK, exit 1 = hung
"""

from gevent import monkey

monkey.patch_all()

import asyncio  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402


def watchdog():  # a real OS thread: patched ones can't run while we hang
    monkey.get_original("time", "sleep")(5)
    sys.__stdout__.write("%s HUNG\n" % sys.version.split()[0])
    sys.__stdout__.flush()
    os._exit(1)


monkey.get_original("_thread", "start_new_thread")(watchdog, ())


async def main():
    first = await asyncio.to_thread(int, "1")  # starts worker asyncio_0
    await asyncio.sleep(0.05)  # the worker goes idle
    return first + await asyncio.to_thread(int, "2")  # reuses it: never wakes


print(sys.version.split()[0], "result", asyncio.run(main()), "OK")
