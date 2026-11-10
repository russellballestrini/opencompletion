# Draft for gevent: `asyncio.to_thread` hangs under `monkey.patch_all()` on CPython 3.13.12+, 3.14.3+ & 3.15

**Status:** draft, not posted. fox decides whether & where it goes
(github.com/gevent/gevent issues). Reproduction beside this file:
`gevent_asyncio_executor_repro.py`. Our own workaround: `gevent_asyncio.py`.

---

## Title

asyncio executor futures never wake their loop under `monkey.patch_all()` on CPython 3.13.12+, 3.14.3+, 3.15

## Summary

After `gevent.monkey.patch_all()`, the second `asyncio.to_thread()` (or
`loop.run_in_executor()`) on an event loop finishes its work & never
returns: the awaiting task stays suspended & the loop sleeps in its
selector. It happens once an executor worker is reused.

It started with a CPython patch release, with gevent unchanged
(26.9.0, the newest release at the time of writing).

## Reproduction

Needs only gevent:

```python
from gevent import monkey

monkey.patch_all()

import asyncio


async def main():
    first = await asyncio.to_thread(int, "1")  # starts worker asyncio_0
    await asyncio.sleep(0.05)                  # the worker goes idle
    return first + await asyncio.to_thread(int, "2")  # reuses it: never wakes


print(asyncio.run(main()))
```

`gevent_asyncio_executor_repro.py` is the same with a watchdog on a real OS
thread, so it exits 1 & prints `HUNG` instead of hanging.

| CPython | Result |
|---|---|
| 3.12.3 | `3` |
| 3.13.11 | `3` |
| 3.13.12 | hangs |
| 3.14.2 | `3` |
| 3.14.3 | hangs |
| 3.14.9 | hangs |
| 3.15.1 | hangs |

gevent 26.9.0, greenlet 3.5.6, Linux x86_64. Patching before or after
importing asyncio makes no difference.

## Cause

`asyncio.futures._chain_future._call_set_state` changed.

Before (3.12, `asyncio/futures.py`):

```python
if dest_loop is None or dest_loop is source_loop:
    _set_state(destination, source)
else:
    dest_loop.call_soon_threadsafe(_set_state, destination, source)
```

For an executor future `source_loop` is `None`, so the result always went
through `call_soon_threadsafe`, which writes the loop's self-pipe & wakes
its selector.

After (3.13.12, 3.14.3, 3.15):

```python
if dest_loop is None or dest_loop is events._get_running_loop():
    _set_state(destination, source)
```

`_get_running_loop()` is per OS thread. Under gevent's patch an executor
"thread" is a greenlet on the loop's own OS thread, so the worker greenlet
sees the loop as running here & sets the result directly. The task's
wake-up lands in `loop._ready`, but the loop is parked in
`gevent/selectors.py` `select()` (`self._ready.wait(timeout)`) & nothing
writes its self-pipe.

A brand new worker runs its first job inside `Thread.start()`, before the
done callback is attached, so the first call works. The hang needs an idle
worker to be reused, which is why it shows on the second call.

State at the hang:

```
Task main() wait_for=<Future finished result=2> cb=[Task.task_wakeup()]
greenlet Thread(asyncio_0): concurrent/futures/thread.py _worker -> work_queue.get(block=True)
main greenlet: asyncio/base_events.py _run_once -> gevent/selectors.py select -> self._ready.wait(timeout)
```

## Who it reaches

Anything that runs asyncio inside a gevent-patched process & touches the
default executor twice on one loop: `asyncio.to_thread`,
`loop.run_in_executor`, `loop.getaddrinfo` (aiohttp's `ThreadedResolver`
shows it as DNS timeouts).

## A workaround that holds for us

Wake the loop when each executor job ends. With real threads it is one
harmless extra wake-up:

```python
import asyncio.base_events as base_events

original = base_events.BaseEventLoop.run_in_executor


def run_in_executor(self, executor, func, *args):
    def job(*job_args):
        try:
            return func(*job_args)
        finally:
            try:
                self._write_to_self()
            except Exception:  # the loop closed while its job ran
                pass

    return original(self, executor, job, *args)


base_events.BaseEventLoop.run_in_executor = run_in_executor
```

With it, the reproduction passes on 3.12, 3.13.12, 3.14.9 & 3.15.1, & a
1,700-test suite that hung on 3.13, 3.14 & 3.15 passes on all three.

A fix inside gevent likely belongs where its patched threads or selector
meet asyncio: either making the patched selector's wait wake when
`loop._ready` gains work from another greenlet, or making a greenlet
"thread" not report the parent's running loop as its own.

## Not checked

- Whether CPython or gevent already track this (we did not search either
  tracker).
- Windows & macOS.
- uvloop & other loop implementations.
