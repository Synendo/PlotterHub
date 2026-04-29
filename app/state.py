import asyncio
import json
import logging
import os
import time
import uuid
from copy import deepcopy
from pathlib import Path

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_PATH = BASE_DIR / "state.json"
UPLOAD_DIR = BASE_DIR / "uploads"

# Jobs that were mid-run when the service died need normalization on load.
# With a valid resume_path on disk the worker's existing res_plot flow can
# continue them; otherwise we can't recover position so they become failed.
# `plotting_calibration` is handled separately — it has no checkpoint, but
# falling back to awaiting_pen_change is harmless (user re-runs calibration
# if they want), so we don't lump it in here.
_IN_FLIGHT_STATUSES = {"optimizing", "planning", "plotting", "homing", "awaiting_pen_change"}

# Permitted job-status transitions, validated centrally in update_job /
# update_job_silent. Same-status updates (no actual transition) are exempt,
# as is the startup rehydrate code in _load_from_disk — that path normalises
# orphaned in-flight statuses by direct mutation, not as a real transition.
_VALID_TRANSITIONS: dict[str, set[str]] = {
    "queued":               {"optimizing", "planning"},
    "optimizing":           {"planning", "cancelled", "failed"},
    "planning":             {"plotting", "cancelled"},
    "plotting":             {"paused", "homing", "awaiting_pen_change",
                             "completed", "failed"},
    "paused":               {"plotting", "homing", "cancelled"},
    "awaiting_pen_change":  {"plotting", "plotting_calibration", "cancelled"},
    "plotting_calibration": {"awaiting_pen_change", "cancelled", "failed"},
    "homing":               {"cancelled"},
    "completed":            {"queued"},
    "failed":               {"queued"},
    "cancelled":            {"queued"},
}


class InvalidTransition(RuntimeError):
    """Raised when update_job is asked to perform a status change that
    violates _VALID_TRANSITIONS. Surfaces real bugs early rather than
    silently corrupting the state machine."""


def _check_status_transition(job_id: str, current: str | None, new: str) -> None:
    if new == current:
        return
    allowed = _VALID_TRANSITIONS.get(current or "", set())
    if new in allowed:
        return
    log.error("state: invalid transition %s → %s for job %s", current, new, job_id)
    raise InvalidTransition(f"job {job_id}: {current!r} → {new!r}")

_queue: list[dict] = []
_active_id: str | None = None
_awaiting_next_job: bool = False
_error: str | None = None

_clients: set = set()
_event_queue: asyncio.Queue | None = None
_loop: asyncio.AbstractEventLoop | None = None


def init(loop: asyncio.AbstractEventLoop) -> None:
    global _event_queue, _loop
    _loop = loop
    _event_queue = asyncio.Queue()
    _load_from_disk()


def _load_from_disk() -> None:
    """Rehydrate the queue from state.json. Called once at startup.

    Skips jobs whose source SVG has been deleted — they're unrecoverable.
    Normalizes statuses so an interrupted plot surfaces as 'paused' (if a
    resume SVG is on disk, OR if it was a clean awaiting_pen_change boundary)
    or 'failed' otherwise, never as 'plotting'.
    """
    global _queue, _active_id
    if not STATE_PATH.exists():
        return
    try:
        data = json.loads(STATE_PATH.read_text())
    except Exception:
        log.exception("state: could not parse %s; starting empty", STATE_PATH)
        return
    raw = data.get("queue") or []
    rehydrated: list[dict] = []
    for job in raw:
        if not isinstance(job, dict) or "job_id" not in job or "svg_id" not in job:
            continue
        if not (UPLOAD_DIR / f"{job['svg_id']}.svg").exists():
            log.info("state: dropping job %s — source SVG missing", job.get("job_id"))
            continue
        status = job.get("status")
        resume_path = job.get("resume_path")
        resume_ok = bool(resume_path) and Path(resume_path).exists()
        if status == "awaiting_pen_change":
            # Clean checkpoint between stages: no resume SVG needed — the next
            # stage will be filtered/rendered from current_stage_index fresh.
            job["status"] = "paused"
            job["resume_path"] = None
        elif status == "plotting_calibration":
            # Calibration has no resume SVG. Treat it like an awaiting_pen_change
            # rehydrate (which has the same shape — clean stage boundary, pen
            # somewhere unknown): mark paused, user resumes, next stage is
            # re-rendered from current_stage_index fresh.
            job["status"] = "paused"
            job["resume_path"] = None
        elif status in _IN_FLIGHT_STATUSES:
            # planning/plotting/homing: pen was somewhere mid-motion. We can
            # recover only if plot_run had time to write a resume SVG.
            if resume_ok:
                job["status"] = "paused"
            else:
                job["status"] = "failed"
                job["error"] = "Service restarted mid-plot before a resume point was reached."
                job["resume_path"] = None
        elif status == "paused" and not resume_ok:
            job["status"] = "failed"
            job["error"] = "Resume data missing after service restart."
            job["resume_path"] = None
        rehydrated.append(job)
    _queue = rehydrated

    # Surface the first paused job as the UI's "active" one so the Resume
    # button is wired up without needing a live worker thread.
    for j in _queue:
        if j["status"] == "paused":
            _active_id = j["job_id"]
            break

    log.info("state: loaded %d job(s) from %s", len(_queue), STATE_PATH)


def _persist() -> None:
    """Atomically write the queue to state.json. Called after every mutation.

    Writes to a sibling tmp file and renames so a crash mid-write can't
    corrupt the file the next boot reads.
    """
    try:
        tmp = STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"queue": _queue}, indent=2) + "\n")
        os.replace(tmp, STATE_PATH)
    except Exception:
        log.exception("state: failed to persist %s", STATE_PATH)


def snapshot() -> dict:
    return {
        "queue": [deepcopy(j) for j in _queue],
        "active_id": _active_id,
        "awaiting_next_job": _awaiting_next_job,
        "status": _derive_top_status(),
        "error": _error,
    }


def _derive_top_status() -> str:
    if _awaiting_next_job:
        return "awaiting_next_job"
    if _active_id is None:
        # Any errored/completed jobs don't count; idle unless queue is non-empty queued
        return "idle"
    job = _get(_active_id)
    return job["status"] if job else "idle"


def _get(job_id: str) -> dict | None:
    for j in _queue:
        if j["job_id"] == job_id:
            return j
    return None


def get_job(job_id: str) -> dict | None:
    j = _get(job_id)
    return deepcopy(j) if j else None


def active_job() -> dict | None:
    return _get(_active_id) if _active_id else None


def add_job(job: dict) -> dict:
    record = _make_record(job)
    _queue.append(record)
    _persist()
    _broadcast()
    return deepcopy(record)


def _make_record(data: dict) -> dict:
    return {
        "job_id": uuid.uuid4().hex[:8],
        "status": "queued",
        "created_at": time.time(),
        "stages": [],
        "current_stage_index": 0,
        "started_at": None,
        "plotting_started_at": None,
        "estimated_total_seconds": None,
        "distance_pendown_m": None,
        "distance_total_m": None,
        "pen_lifts": None,
        "resume_path": None,
        "error": None,
        **data,
    }


def update_job(job_id: str, **updates) -> dict | None:
    j = _get(job_id)
    if j is None:
        return None
    if "status" in updates:
        _check_status_transition(job_id, j.get("status"), updates["status"])
    j.update(updates)
    _persist()
    _broadcast()
    return deepcopy(j)


def update_job_silent(job_id: str, **updates) -> None:
    """Same as update_job but no broadcast — for tight-loop fields we don't need to stream."""
    j = _get(job_id)
    if j is not None:
        if "status" in updates:
            _check_status_transition(job_id, j.get("status"), updates["status"])
        j.update(updates)
        _persist()


def remove_job(job_id: str) -> bool:
    global _queue
    before = len(_queue)
    _queue = [j for j in _queue if j["job_id"] != job_id]
    if len(_queue) < before:
        _persist()
        _broadcast()
        return True
    return False


def move_job(job_id: str, new_index: int) -> bool:
    global _queue
    j = _get(job_id)
    if j is None:
        return False
    _queue = [x for x in _queue if x["job_id"] != job_id]
    new_index = max(0, min(new_index, len(_queue)))
    _queue.insert(new_index, j)
    _persist()
    _broadcast()
    return True


def set_active(job_id: str | None) -> None:
    global _active_id
    _active_id = job_id
    _broadcast()


def set_awaiting_next_job(flag: bool) -> None:
    global _awaiting_next_job
    _awaiting_next_job = flag
    _broadcast()


def set_error(err: str | None) -> None:
    global _error
    _error = err
    _broadcast()


def next_queued_job() -> dict | None:
    for j in _queue:
        if j["status"] == "queued":
            return j
    return None


def next_paused_job() -> dict | None:
    for j in _queue:
        if j["status"] == "paused":
            return j
    return None


def broadcast() -> None:
    _broadcast()


def _broadcast() -> None:
    if _loop is None or _event_queue is None:
        return
    payload = {"type": "state", **snapshot()}
    _loop.call_soon_threadsafe(_event_queue.put_nowait, payload)


def emit_position(x_mm: float, y_mm: float, pen_down: bool) -> None:
    if _loop is None or _event_queue is None:
        return
    payload = {"type": "position", "x_mm": x_mm, "y_mm": y_mm, "pen_down": pen_down}
    _loop.call_soon_threadsafe(_event_queue.put_nowait, payload)


async def drain_events() -> None:
    assert _event_queue is not None
    while True:
        payload = await _event_queue.get()
        text = json.dumps(payload)
        dead = []
        for ws in list(_clients):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            _clients.discard(ws)


def add_client(ws) -> None:
    _clients.add(ws)


def remove_client(ws) -> None:
    _clients.discard(ws)
