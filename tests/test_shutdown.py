"""app/shutdown.py — an exiting worker must not first run every job still
queued on a thread pool (the 2010-box round), or gunicorn kills it."""
import subprocess
import sys
import textwrap
import threading
import time

from app import shutdown


def test_the_flag_is_raised_before_python_waits_for_thread_pools():
    import concurrent.futures.thread as cft
    shutdown.install()
    hooks = threading._threading_atexits          # each wrapped as `lambda: func(...)`: look at what it calls

    def calls(h, f):                             # a functools.partial, or a lambda closing over the function
        if getattr(h, "func", None) is not None:
            return h.func == f
        return any(c.cell_contents == f for c in (getattr(h, "__closure__", None) or ())
                   if callable(getattr(c, "cell_contents", None)))
    ours = max(i for i, h in enumerate(hooks) if calls(h, shutdown._stopping.set))
    theirs = max(i for i, h in enumerate(hooks) if calls(h, cft._python_exit))
    assert ours > theirs                     # threading runs these newest first: ours goes first
    assert cft is not None


def test_a_process_exits_without_running_its_queued_jobs():
    """A real child: 40 queued half-second jobs on one thread, then the main
    thread ends. Without the flag the exit waits ~20 s for the queue."""
    code = textwrap.dedent("""
        import sys, time
        sys.path.insert(0, %r)
        from concurrent.futures import ThreadPoolExecutor
        from app import shutdown
        shutdown.install()
        pool = ThreadPoolExecutor(max_workers=1)
        for _ in range(40):
            pool.submit(lambda: None if shutdown.stopping() else time.sleep(0.5))
        print("queued", flush=True)
    """) % (sys.path[0] if sys.path[0] else ".")
    t = time.monotonic()
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                       cwd=__import__("os").path.dirname(__import__("os").path.dirname(__file__)))
    took = time.monotonic() - t
    assert r.returncode == 0 and "queued" in r.stdout, r.stderr
    assert took < 8, f"the exit waited {took:.1f} s for queued jobs"
