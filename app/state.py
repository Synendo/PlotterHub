import asyncio
import json
import time
import uuid
from copy import deepcopy

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
        if j["id"] == job_id:
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
    _broadcast()
    return deepcopy(record)


def _make_record(data: dict) -> dict:
    return {
        "id": uuid.uuid4().hex[:8],
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
    j.update(updates)
    _broadcast()
    return deepcopy(j)


def update_job_silent(job_id: str, **updates) -> None:
    """Same as update_job but no broadcast — for tight-loop fields we don't need to stream."""
    j = _get(job_id)
    if j is not None:
        j.update(updates)


def remove_job(job_id: str) -> bool:
    global _queue
    before = len(_queue)
    _queue = [j for j in _queue if j["id"] != job_id]
    if len(_queue) < before:
        _broadcast()
        return True
    return False


def move_job(job_id: str, new_index: int) -> bool:
    global _queue
    j = _get(job_id)
    if j is None:
        return False
    _queue = [x for x in _queue if x["id"] != job_id]
    new_index = max(0, min(new_index, len(_queue)))
    _queue.insert(new_index, j)
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
