"""Single-worker FIFO queue for background job planning (preview).

The plan queue runs the same preview/estimate computation that the plot
worker would otherwise do at plot time, but eagerly — kicked off when a job
is created or edited. By the time the user clicks Plot, the estimate is
already on the job record and ``_preview_cache`` is warm so the plot's own
planning step is an instant cache hit.

Optimization comes first: a plan task that needs an optimized SVG calls
``optimize_queue.request_for_job`` (with ``on_running=None`` so the job's
status isn't side-effected — the job stays ``queued``). FIFO order means
the job's own optimize task naturally runs before its plan task.
"""
import logging
import threading
from typing import Iterable

from . import optimize_queue, plot_worker, state

log = logging.getLogger(__name__)


# Plan task --------------------------------------------------------------

class _Task:
    __slots__ = ("job_id", "started", "done", "ok", "error", "cancel_event")

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.started = threading.Event()
        self.done = threading.Event()
        self.ok = False
        self.error: str | None = None
        # Composite cancel: trips on (a) explicit cancel(job_id) calls and (b)
        # shutdown. Forwarded to optimize_queue.request_for_job and into the
        # preview subprocess watcher.
        self.cancel_event = threading.Event()


_pending: list[_Task] = []
_inflight: _Task | None = None
_lock = threading.Lock()
_wakeup = threading.Event()
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()
_shutdown = threading.Event()


# Lifecycle --------------------------------------------------------------

def start() -> None:
    """Start the plan worker thread (idempotent)."""
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _shutdown.clear()
        _thread = threading.Thread(target=_loop, daemon=True, name="plan-queue")
        _thread.start()


def shutdown(timeout_s: float = 5.0) -> None:
    """Signal the worker to drain. Cancels any in-flight task's preview."""
    _shutdown.set()
    _wakeup.set()
    with _lock:
        if _inflight is not None:
            _inflight.cancel_event.set()
    t = _thread
    if t is not None and t.is_alive() and threading.current_thread() is not t:
        t.join(timeout=timeout_s)


def bootstrap_from_state() -> None:
    """Re-enqueue queued jobs whose plan hasn't completed.

    Called once at startup, after state.init. ``plan_status="ready"`` jobs are
    skipped — they already carry a usable estimate from before the restart.
    The in-process ``_preview_cache`` is empty after a restart, so the plot
    worker would re-run preview on a Plot click; that's acceptable since the
    UI estimate was already correct.
    """
    snap = state.snapshot()
    for job in snap["queue"]:
        if job["status"] != "queued":
            continue
        if job.get("plan_status") == "ready" and job.get("estimated_total_seconds"):
            continue
        enqueue(job)


# Public API -------------------------------------------------------------

def enqueue(job: dict) -> None:
    """Queue ``job`` for background planning. Dedup by ``job_id``."""
    job_id = job["job_id"]
    with _lock:
        if _inflight is not None and _inflight.job_id == job_id \
                and not _inflight.cancel_event.is_set():
            return
        for t in _pending:
            if t.job_id == job_id and not t.cancel_event.is_set():
                return
        _pending.append(_Task(job_id))
        _wakeup.set()
    state.update_job(job_id, plan_status="pending")


def cancel(job_id: str) -> None:
    """Drop pending plan tasks for ``job_id`` and cancel an inflight one."""
    with _lock:
        global _pending
        kept: list[_Task] = []
        for t in _pending:
            if t.job_id == job_id:
                t.cancel_event.set()
                t.done.set()
            else:
                kept.append(t)
        _pending = kept
        if _inflight is not None and _inflight.job_id == job_id:
            _inflight.cancel_event.set()


def cancel_for_svg(svg_id: str) -> None:
    """Cancel any plan tasks whose job references ``svg_id`` (used on SVG
    deletion). Looks each pending task's job up to map svg_id → job_ids."""
    job_ids: list[str] = []
    with _lock:
        candidates: Iterable[_Task] = list(_pending)
        if _inflight is not None:
            candidates = [*candidates, _inflight]
        for t in candidates:
            j = state.get_job(t.job_id)
            if j and j.get("svg_id") == svg_id:
                job_ids.append(t.job_id)
    for jid in job_ids:
        cancel(jid)


# Internals --------------------------------------------------------------

def _loop() -> None:
    global _inflight
    while True:
        if _shutdown.is_set():
            return
        with _lock:
            task = _pending.pop(0) if _pending else None
            if task is not None:
                _inflight = task
        if task is None:
            _wakeup.wait()
            _wakeup.clear()
            continue

        try:
            _process(task)
        except Exception:
            log.exception("plan_queue: unexpected error processing job %s", task.job_id)
            task.ok = False
            task.error = "internal error"
        finally:
            with _lock:
                _inflight = None
            task.started.set()
            task.done.set()


def _process(task: _Task) -> None:
    if task.cancel_event.is_set():
        return

    job = state.get_job(task.job_id)
    if job is None:
        return  # job was deleted before we got to it
    if job["status"] != "queued":
        # Plot worker already picked this up (or it's terminal). Don't fight it.
        return

    # Wait for the SVG's optimize task to complete first, if any. on_running=None
    # so we don't flip the job's status — the user's job stays "queued" and
    # plot_status moves through pending → planning → ready.
    if job.get("optimize_svg"):
        settings = optimize_queue.settings_from_job(job)
        ok, err = optimize_queue.request_for_job(
            job["svg_id"], settings, on_running=None, cancel_flag=task.cancel_event,
        )
        if not ok:
            # Either canceled (we'll bail silently) or vpype failed (let plot
            # worker surface the error when the user clicks Plot — we don't
            # have a great UI surface for "background pre-planning failed").
            if not task.cancel_event.is_set():
                state.update_job_silent(task.job_id, plan_status="failed",
                                        plan_error=err)
            return

    # Re-read the job: optimize might have written optimized_with_key, and
    # the user may have edited fields between enqueue and now.
    job = state.get_job(task.job_id)
    if job is None or job["status"] != "queued":
        return
    if task.cancel_event.is_set():
        return

    state.update_job(task.job_id, plan_status="planning")
    task.started.set()

    svg_path = plot_worker._effective_svg_path(job)
    estimate = plot_worker.compute_preview(job, svg_path, cancel_event=task.cancel_event)

    if task.cancel_event.is_set():
        return

    # Job may have been edited or deleted while preview ran.
    current = state.get_job(task.job_id)
    if current is None or current["status"] != "queued":
        return

    if estimate:
        state.update_job(task.job_id, plan_status="ready", **estimate)
        task.ok = True
    else:
        state.update_job_silent(task.job_id, plan_status="failed")
        task.ok = False
