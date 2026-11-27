"""
app/shutdown.py — background work stops promptly when a gunicorn worker exits

On a reload (kill -HUP) gunicorn starts new workers and tells the old ones to
exit. An exiting Python process waits for every thread pool to finish ALL the
work queued on it (concurrent.futures joins its threads at exit, and they
drain the queue first). The 2010-box reader queues ~186 boxes on 16 threads,
so a reload landing mid-round made the old worker run the rest of the round
before exiting: over gunicorn's 60 s limit once (2026-11-26 22:41), and it was
killed. Harmless to visitors (the new workers were already serving), but a
kill can land anywhere.

  install()     once per process, from create_app: raises the flag at the very
                start of interpreter shutdown, BEFORE Python waits for the
                thread pools (threading runs its exit hooks newest first, and
                this one is registered after concurrent.futures' own)
  stopping()    True once the worker is on its way out: queued work skips
                itself, loops stop instead of starting another round

Work already running finishes its current step (a box mid-read finishes the
one request it is waiting on, at most legacy.TIMEOUT), so a worker now exits
within seconds of its last request.
"""
import threading

_stopping = threading.Event()
_installed = False


def stopping() -> bool:
    return _stopping.is_set()


def install() -> None:
    global _installed
    if _installed:
        return
    import concurrent.futures.thread  # noqa: F401  (its join-at-exit hook must be registered before ours)
    register = getattr(threading, "_register_atexit", None)
    if register is not None:
        register(_stopping.set)
    _installed = True


def _set_for_tests(on: bool) -> None:
    (_stopping.set if on else _stopping.clear)()
