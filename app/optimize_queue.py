"""Single-worker FIFO queue for SVG optimization (vpype).

Two entry points:

* ``enqueue_for_upload(svg_id)`` — fire-and-forget, called from ``/upload``.
  Pre-optimizes the SVG using the current config defaults so plot start is
  faster. No-op if ``OPTIMIZE_SVG_DEFAULT`` is off.
* ``request_for_job(svg_id, settings, on_running, cancel_flag)`` — synchronous,
  called from the plot worker. Enqueues the job's optimize task (or joins an
  in-flight matching one) and blocks until it's done. The caller owns the job
  status; we just call back when our task starts running so the caller can
  flip ``awaiting_optimize`` → ``optimizing``.

Why a single shared queue rather than letting both run in parallel: vpype is
CPU-heavy and the device this runs on (a Pi) is small. Two simultaneous vpype
processes would just fight over the cores.

Caching: the cached output ``<svg_id>.opt.svg`` is keyed by the optimize
settings (linemerge / linesimplify / linesort / reloop / tolerance). The same
settings_key is recorded in ``state._svgs[svg_id]`` so a service restart can
tell whether the on-disk file still matches.
"""
import logging
import threading
import time
from pathlib import Path
from typing import Callable

from . import config, state, svg_optimize

log = logging.getLogger(__name__)


def settings_key(settings: dict) -> str:
    """Stable string key for an optimize-settings dict.

    Mirrors the cache key used per-job in plot_worker so a job whose
    ``optimized_with_key`` already matches the on-disk ``.opt.svg`` shortcuts
    around us entirely.
    """
    return "|".join([
        f"t={float(settings['tolerance_mm']):.4f}",
        f"lm={int(bool(settings['linemerge']))}",
        f"ls={int(bool(settings['linesimplify']))}",
        f"so={int(bool(settings['linesort']))}",
        f"rl={int(bool(settings['reloop']))}",
    ])


def settings_from_config() -> dict:
    return {
        "tolerance_mm": float(config.OPTIMIZE_SVG_TOLERANCE_DEFAULT_MM),
        "linemerge": bool(config.OPTIMIZE_SVG_LINEMERGE_DEFAULT),
        "linesimplify": bool(config.OPTIMIZE_SVG_LINESIMPLIFY_DEFAULT),
        "linesort": bool(config.OPTIMIZE_SVG_LINESORT_DEFAULT),
        "reloop": bool(config.OPTIMIZE_SVG_RELOOP_DEFAULT),
    }


def settings_from_job(job: dict) -> dict:
    return {
        "tolerance_mm": float(job.get("optimize_svg_tolerance_mm", 0.10)),
        "linemerge": bool(job.get("optimize_svg_linemerge", True)),
        "linesimplify": bool(job.get("optimize_svg_linesimplify", True)),
        "linesort": bool(job.get("optimize_svg_linesort", True)),
        "reloop": bool(job.get("optimize_svg_reloop", True)),
    }


# Internal task representation --------------------------------------------

class _Task:
    __slots__ = ("svg_id", "settings", "settings_key", "kind",
                 "started", "done", "ok", "error", "cancelled")

    def __init__(self, svg_id: str, settings: dict, sk: str, kind: str) -> None:
        self.svg_id = svg_id
        self.settings = settings
        self.settings_key = sk
        self.kind = kind  # "upload" or "job"
        self.started = threading.Event()
        self.done = threading.Event()
        self.ok = False
        self.error: str | None = None
        self.cancelled = False


_pending: list[_Task] = []
_inflight: _Task | None = None
_lock = threading.Lock()
_wakeup = threading.Event()
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()
_shutdown = threading.Event()

_UPLOAD_DIR_LAZY: Path | None = None


def _uploads() -> Path:
    """Defer the import of UPLOAD_DIR until first call to dodge a startup
    circular: main → optimize_queue → main."""
    global _UPLOAD_DIR_LAZY
    if _UPLOAD_DIR_LAZY is None:
        from .main import UPLOAD_DIR
        _UPLOAD_DIR_LAZY = UPLOAD_DIR
    return _UPLOAD_DIR_LAZY


# Lifecycle ---------------------------------------------------------------

def start() -> None:
    """Start the optimize worker thread (idempotent)."""
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _shutdown.clear()
        _thread = threading.Thread(target=_loop, daemon=True, name="optimize-queue")
        _thread.start()


def shutdown(timeout_s: float = 5.0) -> None:
    """Ask the worker to drain and exit. Kills any in-flight subprocess."""
    _shutdown.set()
    _wakeup.set()
    with _lock:
        if _inflight is not None:
            _inflight.cancelled = True
            svg_optimize.cancel_current()
    t = _thread
    if t is not None and t.is_alive() and threading.current_thread() is not t:
        t.join(timeout=timeout_s)


def bootstrap_from_disk() -> None:
    """Re-enqueue any uploaded SVG that has no usable cached optimization.

    Called once at app startup, after state.init. Scans the uploads dir; for
    every ``<svg_id>.svg``:
      - if there's a fresh ``.opt.svg`` matching current defaults, mark ready;
      - otherwise, if the global default is on, enqueue with current defaults.
    """
    if not config.OPTIMIZE_SVG_DEFAULT:
        return
    uploads = _uploads()
    if not uploads.exists():
        return
    defaults = settings_from_config()
    sk = settings_key(defaults)
    for svg in uploads.iterdir():
        if not svg.is_file() or svg.suffix != ".svg":
            continue
        # Skip derivative files (.opt, .preview, .filt, .resume, .combined.filt, .s0.filt, ...).
        # The original upload's stem is exactly the svg_id (8 hex chars, no dots).
        if "." in svg.stem:
            continue
        svg_id = svg.stem
        opt = svg.with_name(f"{svg_id}.opt.svg")
        existing = state.get_svg_status(svg_id)
        if opt.exists() and existing and existing.get("settings_key") == sk \
                and existing.get("status") == "ready":
            continue
        # Fire-and-forget; the worker will handle it in order.
        _enqueue(svg_id, defaults, kind="upload")


# Public API --------------------------------------------------------------

def enqueue_for_upload(svg_id: str) -> None:
    """Pre-optimize using current defaults. No-op if the global toggle is off."""
    if not config.OPTIMIZE_SVG_DEFAULT:
        return
    _enqueue(svg_id, settings_from_config(), kind="upload")


def enqueue_for_job(job: dict) -> None:
    """Pre-optimize using a freshly-created job's actual settings.

    Fire-and-forget: the plot worker still calls ``request_for_job`` later and
    will dedup onto our task if it's still inflight, or hit the cache if it
    finished. This closes the common "API client sends custom settings; the
    upload-time pre-opt with config defaults misses cache" case.

    No-op if the job has optimize_svg disabled — there's nothing to do.
    """
    if not job.get("optimize_svg"):
        return
    _enqueue(job["svg_id"], settings_from_job(job), kind="job")


def request_for_job(svg_id: str,
                    settings: dict,
                    on_running: Callable[[], None] | None,
                    cancel_flag: threading.Event) -> tuple[bool, str | None]:
    """Block until the optimize for ``svg_id`` with these settings is done.

    Returns ``(ok, error)``. Honours ``cancel_flag``: if set while pending the
    task is dropped; if set while running the subprocess is killed.
    ``on_running`` fires the moment our task actually starts (so the caller
    can flip the job from ``awaiting_optimize`` → ``optimizing``).
    """
    task = _enqueue(svg_id, settings, kind="job")
    fired = False

    while not task.done.is_set():
        if cancel_flag.is_set():
            _cancel_task(task)
            break
        if task.started.is_set() and not fired and on_running is not None:
            on_running()
            fired = True
        # Short timeout so we re-check cancel_flag promptly.
        task.done.wait(timeout=0.1)

    # If the task finished before we noticed `started` (cache fast-path), still
    # fire the callback so the caller doesn't see the status flicker past
    # awaiting_optimize.
    if not fired and task.ok and on_running is not None:
        on_running()

    if task.cancelled:
        return False, "cancelled"
    return task.ok, task.error


def cancel(svg_id: str) -> None:
    """Drop any pending tasks for ``svg_id`` and kill an in-flight one.

    Called when an SVG is deleted: there's no point optimizing a file the user
    just removed, and a stale ``.opt.svg`` would be cleaned up by
    ``delete_svg_files`` anyway.
    """
    woken_a_waiter = False
    with _lock:
        global _pending
        kept = []
        for t in _pending:
            if t.svg_id == svg_id:
                t.cancelled = True
                t.done.set()
                woken_a_waiter = True
            else:
                kept.append(t)
        _pending = kept
        if _inflight is not None and _inflight.svg_id == svg_id:
            _inflight.cancelled = True
            svg_optimize.cancel_current()
            woken_a_waiter = True
    state.clear_svg_status(svg_id)
    if woken_a_waiter:
        _wakeup.set()


# Internals ---------------------------------------------------------------

def _opt_path(svg_id: str) -> Path:
    return _uploads() / f"{svg_id}.opt.svg"


def _src_path(svg_id: str) -> Path:
    return _uploads() / f"{svg_id}.svg"


def _enqueue(svg_id: str, settings: dict, kind: str) -> _Task:
    """Create-or-join. Always returns a task whose ``done`` event will fire."""
    sk = settings_key(settings)

    # Fast path: cached output exists and the recorded key still matches → done.
    opt = _opt_path(svg_id)
    existing = state.get_svg_status(svg_id)
    if opt.exists() and existing and existing.get("settings_key") == sk \
            and existing.get("status") == "ready":
        t = _Task(svg_id, settings, sk, kind)
        t.started.set()
        t.ok = True
        t.done.set()
        return t

    with _lock:
        # Dedup against in-flight task with matching key.
        if _inflight is not None and _inflight.svg_id == svg_id \
                and _inflight.settings_key == sk and not _inflight.cancelled:
            return _inflight
        # Dedup against an already-pending matching task.
        for t in _pending:
            if t.svg_id == svg_id and t.settings_key == sk and not t.cancelled:
                return t

        # When a job-task arrives, drop any older pending upload-tasks for the
        # same SVG with different settings — their result would just be
        # overwritten. (We don't kill an in-flight upload-task with different
        # settings; let it finish, then we run.)
        if kind == "job":
            kept: list[_Task] = []
            for t in _pending:
                if t.svg_id == svg_id and t.settings_key != sk and t.kind == "upload":
                    t.cancelled = True
                    t.done.set()
                else:
                    kept.append(t)
            _pending[:] = kept

        new_task = _Task(svg_id, settings, sk, kind)
        _pending.append(new_task)
        _wakeup.set()

    # Don't downgrade an in-flight "optimizing" entry — another task for this
    # SVG is currently running vpype, and the UI is more useful showing that
    # than a generic "pending". The worker writes the new settings_key when
    # it picks our task up.
    existing = state.get_svg_status(svg_id)
    if not (existing and existing.get("status") == "optimizing"):
        state.set_svg_status(svg_id, "pending", settings_key=sk)
    return new_task


def _cancel_task(task: _Task) -> None:
    """Remove ``task`` if pending, or kill it if it's running."""
    drop_status = False
    with _lock:
        if task in _pending:
            _pending.remove(task)
            task.cancelled = True
            task.done.set()
            # If this was the only pending/inflight task for the SVG, the
            # "pending" entry the task left in state.svgs is now stale and
            # would otherwise stick in the UI until the next event.
            drop_status = (
                (_inflight is None or _inflight.svg_id != task.svg_id) and
                not any(t.svg_id == task.svg_id for t in _pending)
            )
        elif _inflight is task:
            task.cancelled = True
            svg_optimize.cancel_current()
            # The worker loop's finally clause will set task.done; _process
            # also clears state on cancellation.
    if drop_status:
        state.clear_svg_status(task.svg_id)


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
            log.exception("optimize_queue: unexpected error processing %s", task.svg_id)
            task.ok = False
            task.error = "internal error"
        finally:
            with _lock:
                _inflight = None
            task.started.set()  # idempotent — guarantees waiters unblock
            task.done.set()


def _process(task: _Task) -> None:
    if task.cancelled:
        return
    src = _src_path(task.svg_id)
    if not src.exists():
        # File was deleted between enqueue and dispatch.
        task.ok = False
        task.error = "source SVG missing"
        state.clear_svg_status(task.svg_id)
        return

    state.set_svg_status(task.svg_id, "optimizing", settings_key=task.settings_key)
    task.started.set()

    opt = _opt_path(task.svg_id)
    try:
        svg_optimize.optimize_svg(src, opt, **task.settings)
    except svg_optimize.OptimizeError as e:
        # Clean up a partial file if vpype died mid-write.
        if opt.exists():
            try:
                opt.unlink()
            except OSError:
                pass
        if task.cancelled:
            task.ok = False
            task.error = "cancelled"
            state.clear_svg_status(task.svg_id)
            return
        task.ok = False
        task.error = str(e)
        state.set_svg_status(task.svg_id, "failed",
                             settings_key=task.settings_key, error=str(e))
        return

    if task.cancelled:
        # Cancelled mid-write but vpype somehow returned 0 anyway — discard the
        # output to avoid leaving a half-meaningful file behind.
        if opt.exists():
            try:
                opt.unlink()
            except OSError:
                pass
        task.ok = False
        task.error = "cancelled"
        state.clear_svg_status(task.svg_id)
        return

    task.ok = True
    state.set_svg_status(task.svg_id, "ready", settings_key=task.settings_key)
