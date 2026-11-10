"""asyncio beside gevent's monkey patch: an executor job wakes its loop.

After monkey.patch_all() an executor "thread" is a greenlet on its event
loop's own OS thread. CPython 3.13.12, 3.14.3 & 3.15 changed
asyncio.futures._chain_future to finish a future directly when the loop
is "running in this thread" (true for that greenlet), without the write
to the loop's self-pipe that used to wake it. So the second
asyncio.to_thread / run_in_executor on a loop (the first one to reuse an
idle worker) finished its work & never returned: the loop slept on.
gevent 26.9.0, its newest, has no fix. Found 2026-11-10: our crawler's
tests hung CI on 3.13 & 3.14, & an earlier getaddrinfo hang was this too.

wake_executor_jobs() makes every executor job end by waking its loop. With
real threads that is one harmless extra wake-up. Call it wherever
monkey.patch_all() is called.
"""

import asyncio.base_events as _base_events


def wake_executor_jobs():
    """Patch BaseEventLoop.run_in_executor once; safe to call again."""
    original = _base_events.BaseEventLoop.run_in_executor
    if getattr(original, "wakes_its_loop", False):
        return

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

    run_in_executor.wakes_its_loop = True
    _base_events.BaseEventLoop.run_in_executor = run_in_executor
