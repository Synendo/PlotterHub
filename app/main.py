import asyncio
import json as _json
import logging
import subprocess
import uuid

log = logging.getLogger(__name__)
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import (
    Depends, FastAPI, File, Form, Header, HTTPException,
    UploadFile, WebSocket, WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from . import config, optimize_queue, plan_queue, plot_worker, state, svg_utils, updates

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
STATIC_DIR = BASE_DIR / "static"

# Mirror of static/app.js PAPER_SIZES — portrait dims in mm. Used by the
# /api/v1/jobs endpoint to resolve paper_size.name into dimensions.
PAPER_PRESETS: dict[str, tuple[float, float]] = {
    "A0": (841, 1189), "A1": (594, 841), "A2": (420, 594),
    "A3": (297, 420),  "A4": (210, 297), "A5": (148, 210),
    "B0": (1000, 1414), "B1": (707, 1000), "B2": (500, 707),
    "B3": (353, 500),  "B4": (250, 353), "B5": (176, 250),
    "Letter": (216, 279), "Legal": (216, 356), "Ledger": (279, 432),
    "ANSI-C": (432, 559), "ANSI-D": (559, 864), "ANSI-E": (864, 1118),
}

LENGTH_UNIT_TO_MM: dict[str, float] = {"mm": 1.0, "cm": 10.0, "in": 25.4}


# Web-UI-facing errors carry a stable {code, params} detail instead of an
# English string, so the browser can localize them (see apiErrText in app.js).
# Only the unprefixed (web UI) routes use these; the /api/v1/* routes keep
# their plain-string details for external clients.
def _coded(status: int, code: str, **params) -> HTTPException:
    detail: dict = {"code": code}
    if params:
        detail["params"] = params
    return HTTPException(status, detail=detail)


# The plot worker raises RuntimeError with a stable message; map the known ones
# to codes, and fall back to a generic code that carries the raw text.
_WORKER_ERROR_CODES: dict[str, str] = {
    "No active plot": "no_active_plot",
    "No paused job to resume": "no_paused_job",
    "No resume data": "no_resume_data",
    "Nothing to continue": "nothing_to_continue",
    "Calibration plot only available at a pen-change pause": "calibrate_not_at_pause",
    "This job has no calibration layers": "no_calibration_layers",
    "No active job": "no_active_job",
}


def _worker_error(e: RuntimeError) -> HTTPException:
    code = _WORKER_ERROR_CODES.get(str(e))
    if code:
        return _coded(409, code)
    return _coded(409, "worker_error", detail=str(e))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    state.init(asyncio.get_running_loop())
    optimize_queue.start()
    plan_queue.start()
    optimize_queue.bootstrap_from_disk()
    plan_queue.bootstrap_from_state()
    drain_task = asyncio.create_task(state.drain_events())
    try:
        yield
    finally:
        await asyncio.get_running_loop().run_in_executor(None, plot_worker.shutdown_gracefully)
        # Tear down preprocessing workers after the plot worker so any
        # in-flight upload pre-opt or background plan finishes cleanly when
        # nothing else is competing for CPU.
        plan_queue.shutdown()
        optimize_queue.shutdown()
        drain_task.cancel()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    # Stamp the running version onto the asset URLs so a browser always fetches
    # fresh app.js / style.css after a self-update instead of serving a stale
    # cached copy. Read per request so static edits show up without a restart.
    html = (STATIC_DIR / "index.html").read_text()
    return HTMLResponse(html.replace("__ASSET_VERSION__", config.APP_VERSION))


# SVG storage -------------------------------------------------------------

@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    data = await file.read()
    # Sniff the bytes before writing: SVG starts with '<' (optionally after a
    # UTF-8 BOM and whitespace). Anything binary (JPG/PNG/PDF/...) fails fast
    # with a clean message rather than the lxml parse trace.
    head = data.lstrip(b"\xef\xbb\xbf").lstrip()
    if not head.startswith(b"<"):
        raise _coded(400, "not_svg")
    svg_id = uuid.uuid4().hex[:8]
    path = UPLOAD_DIR / f"{svg_id}.svg"
    path.write_bytes(data)
    try:
        info = svg_utils.parse_layers(path)
    except Exception:
        path.unlink(missing_ok=True)
        raise _coded(400, "invalid_svg")
    optimize_queue.enqueue_for_upload(svg_id)
    return {"id": svg_id, "filename": file.filename or "upload.svg", **info}


@app.get("/svg/{svg_id}")
def get_svg(svg_id: str):
    path = UPLOAD_DIR / f"{svg_id}.svg"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(str(path), media_type="image/svg+xml")


# Jobs -------------------------------------------------------------------
#
# The optimize_svg_* fields appear in three shapes:
#   - JobCreate (web POST):           required-with-defaults
#   - JobUpdate (web PATCH):          all-Optional, no defaults
#   - ApiJobMetadata (public POST):   all-Optional, server fills missing values
# Two mixins capture the variants so we don't repeat the field list three times.

class _OptimizeCreateFields(BaseModel):
    optimize_svg: bool = False
    optimize_svg_tolerance_mm: float = 0.10
    optimize_svg_linemerge: bool = True
    optimize_svg_linesimplify: bool = True
    optimize_svg_linesort: bool = True
    optimize_svg_reloop: bool = True


class _OptimizeOptionalFields(BaseModel):
    optimize_svg: bool | None = None
    optimize_svg_tolerance_mm: float | None = None
    optimize_svg_linemerge: bool | None = None
    optimize_svg_linesimplify: bool | None = None
    optimize_svg_linesort: bool | None = None
    optimize_svg_reloop: bool | None = None


class JobCreate(_OptimizeCreateFields):
    svg_id: str
    filename: str = "upload.svg"
    name: str | None = None
    paper_size_name: str | None = None
    layer_selections: list[dict]
    pause_between_layers: bool = True
    pause_after_job: bool = True
    delete_on_complete: bool = False
    paper_width_mm: float
    paper_height_mm: float
    margin_top_mm: float = 0.0
    margin_right_mm: float = 0.0
    margin_bottom_mm: float = 0.0
    margin_left_mm: float = 0.0
    fit_content: bool = False
    transform_scale: float = 1.0
    transform_rotation_deg: float = 0.0
    transform_offset_x_mm: float = 0.0
    transform_offset_y_mm: float = 0.0
    speed_pendown: int = 25
    speed_penup: int = 75
    acceleration: int = 75


class MoveRequest(BaseModel):
    new_index: int = Field(..., ge=0)


class SettingsUpdate(BaseModel):
    plotter_model: int | None = Field(None, ge=1, le=8)
    pause_between_layers_default: bool | None = None
    pause_after_job_default: bool | None = None
    delete_on_complete_default: bool | None = None
    speed_pendown_default: int | None = Field(None, ge=1, le=110)
    speed_penup_default: int | None = Field(None, ge=1, le=110)
    acceleration_default: int | None = Field(None, ge=1, le=100)
    optimize_svg_default: bool | None = None
    optimize_svg_tolerance_default_mm: float | None = Field(None, ge=0.01, le=10.0)
    optimize_svg_linemerge_default: bool | None = None
    optimize_svg_linesimplify_default: bool | None = None
    optimize_svg_linesort_default: bool | None = None
    optimize_svg_reloop_default: bool | None = None
    display_unit: Literal["mm", "cm", "in"] | None = None


# Numeric job fields that get clamped on create/update. Out-of-range values
# are silently corrected to the nearest bound rather than 400'd — a slider
# overshoot or stale client default shouldn't break the request, the user
# clearly wanted the value at the limit.
_CLAMP_RANGES: dict[str, tuple[float, float]] = {
    "speed_pendown": (1, 110),
    "speed_penup": (1, 110),
    "acceleration": (1, 100),
    "transform_scale": (0.01, 5.0),
    "transform_rotation_deg": (0.0, 360.0),
    "optimize_svg_tolerance_mm": (0.01, 10.0),
}


def _clamp_job_fields(d: dict,
                      paper_width_mm: float | None = None,
                      paper_height_mm: float | None = None) -> None:
    """In-place clamp of numeric job fields. ``paper_*_mm`` (when known)
    bounds the transform offsets to the paper extent."""
    for key, (lo, hi) in _CLAMP_RANGES.items():
        v = d.get(key)
        if v is not None:
            d[key] = max(lo, min(hi, v))
    for k in ("margin_top_mm", "margin_right_mm",
              "margin_bottom_mm", "margin_left_mm"):
        v = d.get(k)
        if v is not None:
            d[k] = max(0.0, v)
    if paper_width_mm:
        v = d.get("transform_offset_x_mm")
        if v is not None:
            d["transform_offset_x_mm"] = max(-paper_width_mm,
                                             min(paper_width_mm, v))
    if paper_height_mm:
        v = d.get("transform_offset_y_mm")
        if v is not None:
            d["transform_offset_y_mm"] = max(-paper_height_mm,
                                             min(paper_height_mm, v))


class JobUpdate(_OptimizeOptionalFields):
    layer_selections: list[dict] | None = None
    name: str | None = None
    paper_size_name: str | None = None
    pause_between_layers: bool | None = None
    pause_after_job: bool | None = None
    delete_on_complete: bool | None = None
    paper_width_mm: float | None = None
    paper_height_mm: float | None = None
    margin_top_mm: float | None = None
    margin_right_mm: float | None = None
    margin_bottom_mm: float | None = None
    margin_left_mm: float | None = None
    fit_content: bool | None = None
    transform_scale: float | None = None
    transform_rotation_deg: float | None = None
    transform_offset_x_mm: float | None = None
    transform_offset_y_mm: float | None = None
    speed_pendown: int | None = None
    speed_penup: int | None = None
    acceleration: int | None = None


@app.post("/jobs")
def create_job(req: JobCreate):
    path = UPLOAD_DIR / f"{req.svg_id}.svg"
    if not path.exists():
        raise _coded(404, "svg_not_found")
    if not any(s.get("selected", True) for s in (req.layer_selections or [])):
        raise _coded(400, "select_one_layer")
    payload = req.model_dump()
    _clamp_job_fields(payload, payload.get("paper_width_mm"),
                      payload.get("paper_height_mm"))
    job = state.add_job(payload)
    optimize_queue.enqueue_for_job(job)
    plan_queue.enqueue(job)
    return job


@app.get("/jobs")
def list_jobs():
    return state.snapshot()


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    j = state.get_job(job_id)
    if j is None:
        raise HTTPException(404)
    return j


@app.patch("/jobs/{job_id}")
def update_job(job_id: str, req: JobUpdate):
    j = state.get_job(job_id)
    if j is None:
        raise _coded(404, "job_not_found")
    if j["status"] not in ("queued", "completed", "failed", "cancelled"):
        raise _coded(409, "cannot_edit_active")
    # exclude_unset so the client distinguishes "not sent" from "explicitly null"
    # — needed e.g. for paper_size_name which can be cleared back to None.
    updates = req.model_dump(exclude_unset=True)
    # Use the new paper dims if they're being changed; otherwise fall back to
    # the existing job's so transform offsets clamp against the right extent.
    paper_w = updates.get("paper_width_mm", j.get("paper_width_mm"))
    paper_h = updates.get("paper_height_mm", j.get("paper_height_mm"))
    _clamp_job_fields(updates, paper_w, paper_h)
    # Re-queue on edit so user can re-plot a finished/cancelled job without extra steps
    if j["status"] != "queued":
        updates["status"] = "queued"
        updates["error"] = None
    # Any edit can change the preview cache key, so the on-record estimate is
    # potentially stale. Drop it; plan_queue will recompute.
    updates.update({
        "estimated_total_seconds": None,
        "distance_pendown_m": None,
        "distance_total_m": None,
        "pen_lifts": None,
        "plan_status": None,
    })
    state.update_job(job_id, **updates)
    fresh = state.get_job(job_id)
    plan_queue.cancel(job_id)
    optimize_queue.enqueue_for_job(fresh)
    plan_queue.enqueue(fresh)
    return fresh


def delete_svg_files(svg_id: str | None) -> None:
    # Delete the source SVG and every derivative (preview / filtered / staged /
    # resume). svg_id is a uuid4 fragment, 1:1 with a job, so globbing on it
    # can't hit another job's files.
    if not svg_id:
        return
    # Drop any pending or in-flight preprocessing first so the workers don't
    # race us by writing a fresh .opt.svg / .preview.svg right after we unlink.
    optimize_queue.cancel(svg_id)
    plan_queue.cancel_for_svg(svg_id)
    for p in UPLOAD_DIR.glob(f"{svg_id}.*"):
        try:
            p.unlink()
        except OSError:
            log.exception("delete_svg_files: failed to unlink %s", p)


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str):
    j = state.get_job(job_id)
    if j is None:
        raise _coded(404, "job_not_found")
    if j["status"] in ("plotting", "planning", "paused", "awaiting_pen_change", "homing"):
        raise _coded(409, "cannot_remove_active")
    svg_id = j.get("svg_id")
    state.remove_job(job_id)
    delete_svg_files(svg_id)
    return {"ok": True}


# Public API (v1) -----------------------------------------------------------
# Routes under /api/v1/* are intended for external clients (e.g. the macOS
# companion app). They require the X-API-Key header. The web UI uses the
# unprefixed routes above (loopback, no auth).

def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    if not config.API_KEY:
        raise HTTPException(503, "API key not initialized")
    if x_api_key != config.API_KEY:
        raise HTTPException(401, "invalid or missing X-API-Key")


class ApiPaperSize(BaseModel):
    name: str | None = None
    width: float | None = None
    height: float | None = None
    unit: Literal["mm", "cm", "in"] = "mm"
    orientation: Literal["portrait", "landscape"] | None = None


class ApiLayer(BaseModel):
    index: int = Field(ge=0)
    name: str | None = None
    type: Literal["pattern", "text", "svg", "calibration"] | None = None
    selected: bool | None = None  # None == not specified == default True
    # Optional per-layer speed overrides. When set, they take precedence over
    # the job (document) / system speeds for that layer — see _run_job, which
    # forces per-layer staging when any override is present.
    speed_pendown: int | None = None
    speed_penup: int | None = None
    acceleration: int | None = None


class ApiJobMetadata(_OptimizeOptionalFields):
    name: str | None = None
    paper_size: ApiPaperSize | None = None
    layers: list[ApiLayer] = Field(default_factory=list)
    pause_between_layers: bool | None = None
    pause_after_job: bool | None = None
    delete_on_complete: bool | None = None
    speed_pendown: int | None = None
    speed_penup: int | None = None
    acceleration: int | None = None
    # Request-only directive: when true AND the queue is empty at the moment of
    # the POST, kick off the worker so this job plots immediately. Not stored
    # on the job record.
    auto_plot: bool = False


def _resolve_paper(paper: ApiPaperSize | None,
                   svg_w_mm: float | None,
                   svg_h_mm: float | None) -> tuple[float, float, str | None]:
    """Return (paper_width_mm, paper_height_mm, display_name)."""
    if paper is None:
        # Auto-detect from SVG dimensions, like the web UI does on a fresh upload.
        return float(svg_w_mm or 210.0), float(svg_h_mm or 297.0), None

    factor = LENGTH_UNIT_TO_MM[paper.unit]
    w_mm: float | None = paper.width * factor if paper.width is not None else None
    h_mm: float | None = paper.height * factor if paper.height is not None else None

    if w_mm is None or h_mm is None:
        # Fall back to the named preset.
        if paper.name and paper.name in PAPER_PRESETS:
            pw, ph = PAPER_PRESETS[paper.name]
            w_mm, h_mm = float(pw), float(ph)
        elif paper.name:
            raise HTTPException(400, f"unknown paper preset: {paper.name!r}")
        else:
            raise HTTPException(400, "paper_size requires either width+height or a known name")

    if paper.orientation == "landscape" and w_mm < h_mm:
        w_mm, h_mm = h_mm, w_mm
    elif paper.orientation == "portrait" and w_mm > h_mm:
        w_mm, h_mm = h_mm, w_mm

    return w_mm, h_mm, paper.name


@app.post("/api/v1/jobs", dependencies=[Depends(require_api_key)])
async def api_create_job(file: UploadFile = File(...),
                         metadata: str | None = Form(default=None)):
    # Parse + validate metadata (the part is a JSON string in multipart/form-data).
    if metadata:
        try:
            meta_dict = _json.loads(metadata)
        except _json.JSONDecodeError as e:
            raise HTTPException(400, f"metadata is not valid JSON: {e.msg}")
        try:
            meta = ApiJobMetadata.model_validate(meta_dict)
        except ValidationError as e:
            raise HTTPException(400, f"metadata schema error: {e.errors()}")
    else:
        meta = ApiJobMetadata()

    # Persist the SVG (mirrors /upload).
    svg_id = uuid.uuid4().hex[:8]
    path = UPLOAD_DIR / f"{svg_id}.svg"
    path.write_bytes(await file.read())
    try:
        info = svg_utils.parse_layers(path)
    except Exception as e:
        path.unlink(missing_ok=True)
        raise HTTPException(400, f"invalid SVG: {e}")
    optimize_queue.enqueue_for_upload(svg_id)

    paper_width_mm, paper_height_mm, paper_name = _resolve_paper(
        meta.paper_size, info.get("width_mm"), info.get("height_mm"),
    )

    # Build layer_selections: include every SVG layer, applying per-layer
    # name/type/selected overrides from metadata (keyed by SVG layer index).
    # Deselected layers are kept in the list with `selected: false` so their
    # name/type metadata survives a UI toggle off-and-on. The worker filters
    # by `selected` when planning the plot.
    overrides = {l.index: l for l in meta.layers}
    layer_selections: list[dict] = []
    for layer in info["layers"]:
        idx = layer["index"]
        ovr = overrides.get(idx)
        sel: dict = {"index": idx, "label": (ovr.name if ovr and ovr.name else layer["label"])}
        if ovr and ovr.type:
            sel["type"] = ovr.type
        if ovr and ovr.selected is False:
            sel["selected"] = False
        # Optional per-layer speed overrides — clamped to the same ranges as
        # the job-level speeds (out-of-range values corrected, not rejected).
        for key in ("speed_pendown", "speed_penup", "acceleration"):
            val = getattr(ovr, key) if ovr else None
            if val is not None:
                lo, hi = _CLAMP_RANGES[key]
                sel[key] = int(max(lo, min(hi, val)))
        layer_selections.append(sel)

    if not info["layers"]:
        path.unlink(missing_ok=True)
        raise HTTPException(400, "SVG contains no Inkscape layers")
    if not any(s.get("selected", True) for s in layer_selections):
        path.unlink(missing_ok=True)
        raise HTTPException(400, "all layers were deselected")

    def pick(meta_val, default):
        return default if meta_val is None else meta_val

    job_payload = {
        "svg_id": svg_id,
        "filename": file.filename or "upload.svg",
        "name": meta.name,
        "paper_size_name": paper_name,
        "layer_selections": layer_selections,
        "pause_between_layers": pick(meta.pause_between_layers, config.PAUSE_BETWEEN_LAYERS_DEFAULT),
        "pause_after_job": pick(meta.pause_after_job, config.PAUSE_AFTER_JOB_DEFAULT),
        "delete_on_complete": pick(meta.delete_on_complete, config.DELETE_ON_COMPLETE_DEFAULT),
        "paper_width_mm": paper_width_mm,
        "paper_height_mm": paper_height_mm,
        "margin_top_mm": 0.0,
        "margin_right_mm": 0.0,
        "margin_bottom_mm": 0.0,
        "margin_left_mm": 0.0,
        "fit_content": False,
        "transform_scale": 1.0,
        "transform_rotation_deg": 0.0,
        "transform_offset_x_mm": 0.0,
        "transform_offset_y_mm": 0.0,
        "speed_pendown": pick(meta.speed_pendown, config.SPEED_PENDOWN_DEFAULT),
        "speed_penup": pick(meta.speed_penup, config.SPEED_PENUP_DEFAULT),
        "acceleration": pick(meta.acceleration, config.ACCELERATION_DEFAULT),
        "optimize_svg": pick(meta.optimize_svg, config.OPTIMIZE_SVG_DEFAULT),
        "optimize_svg_tolerance_mm": pick(meta.optimize_svg_tolerance_mm, config.OPTIMIZE_SVG_TOLERANCE_DEFAULT_MM),
        "optimize_svg_linemerge": pick(meta.optimize_svg_linemerge, config.OPTIMIZE_SVG_LINEMERGE_DEFAULT),
        "optimize_svg_linesimplify": pick(meta.optimize_svg_linesimplify, config.OPTIMIZE_SVG_LINESIMPLIFY_DEFAULT),
        "optimize_svg_linesort": pick(meta.optimize_svg_linesort, config.OPTIMIZE_SVG_LINESORT_DEFAULT),
        "optimize_svg_reloop": pick(meta.optimize_svg_reloop, config.OPTIMIZE_SVG_RELOOP_DEFAULT),
    }
    _clamp_job_fields(job_payload, paper_width_mm, paper_height_mm)
    # auto_plot: only kick the worker if no other job is in a runnable or
    # in-progress state (queued / paused / plotting / planning / optimizing /
    # homing / awaiting_pen_change). Terminal-state leftovers (completed /
    # failed / cancelled) don't block — they're inert.
    _TERMINAL = {"completed", "failed", "cancelled"}
    blockers_present = any(
        j["status"] not in _TERMINAL for j in state.snapshot()["queue"]
    )
    job = state.add_job(job_payload)
    optimize_queue.enqueue_for_job(job)
    plan_queue.enqueue(job)
    if meta.auto_plot and not blockers_present:
        plot_worker.start_queue()
    return job


# Queue control (public) ---------------------------------------------------
# Thin wrappers around the existing /queue/* routes with auth bolted on.

@app.post("/api/v1/queue/plot", dependencies=[Depends(require_api_key)])
def api_queue_plot():
    if not any(j["status"] == "queued" for j in state.snapshot()["queue"]):
        raise HTTPException(409, "no queued job to plot")
    active = state.active_job()
    if active is not None and active["status"] in (
        "plotting", "planning", "paused", "awaiting_pen_change", "homing",
    ):
        raise HTTPException(409, "queue is already running")
    plot_worker.start_queue()
    return {"ok": True}


@app.post("/api/v1/queue/pause", dependencies=[Depends(require_api_key)])
def api_queue_pause():
    job = state.active_job()
    if job is None or job["status"] != "plotting":
        raise HTTPException(409, "no active plotting job")
    plot_worker.pause_active()
    return {"ok": True}


@app.post("/api/v1/queue/resume", dependencies=[Depends(require_api_key)])
def api_queue_resume():
    try:
        plot_worker.resume_active()
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/v1/queue/continue", dependencies=[Depends(require_api_key)])
def api_queue_continue():
    try:
        plot_worker.continue_next()
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/v1/queue/cancel", dependencies=[Depends(require_api_key)])
def api_queue_cancel():
    try:
        plot_worker.cancel_active()
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/v1/queue/calibrate", dependencies=[Depends(require_api_key)])
def api_queue_calibrate():
    try:
        plot_worker.trigger_calibration()
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


# Per-job CRUD (public) ----------------------------------------------------
# Thin auth-gated wrappers around the internal handlers above.

@app.get("/api/v1/jobs", dependencies=[Depends(require_api_key)])
def api_list_jobs():
    return list_jobs()


@app.get("/api/v1/jobs/{job_id}", dependencies=[Depends(require_api_key)])
def api_get_job(job_id: str):
    return get_job(job_id)


@app.patch("/api/v1/jobs/{job_id}", dependencies=[Depends(require_api_key)])
def api_update_job(job_id: str, req: JobUpdate):
    return update_job(job_id, req)


@app.delete("/api/v1/jobs/{job_id}", dependencies=[Depends(require_api_key)])
def api_delete_job(job_id: str):
    return delete_job(job_id)


@app.post("/api/v1/jobs/{job_id}/move", dependencies=[Depends(require_api_key)])
def api_move_job(job_id: str, req: MoveRequest):
    return move_job(job_id, req)


@app.post("/api/v1/jobs/{job_id}/requeue", dependencies=[Depends(require_api_key)])
def api_requeue_job(job_id: str):
    return requeue_job(job_id)


# Settings (public) --------------------------------------------------------

@app.get("/api/v1/settings", dependencies=[Depends(require_api_key)])
def api_get_settings():
    # Drop api_key — the caller already needed it to authenticate this request,
    # so re-emitting it would just be noise.
    snap = get_settings()
    snap.pop("api_key", None)
    return snap


@app.patch("/api/v1/settings", dependencies=[Depends(require_api_key)])
def api_patch_settings(req: SettingsUpdate):
    snap = patch_settings(req)
    snap.pop("api_key", None)
    return snap


# System (public) ---------------------------------------------------------

@app.get("/api/v1/version", dependencies=[Depends(require_api_key)])
def api_get_version():
    return get_version()


@app.post("/api/v1/system/shutdown", dependencies=[Depends(require_api_key)])
async def api_system_shutdown():
    return await system_shutdown()


@app.post("/jobs/{job_id}/move")
def move_job(job_id: str, req: MoveRequest):
    j = state.get_job(job_id)
    if j is None:
        raise _coded(404, "job_not_found")
    if j["status"] in ("plotting", "planning", "paused", "awaiting_pen_change", "homing"):
        raise _coded(409, "cannot_move_active")
    state.move_job(job_id, req.new_index)
    return {"ok": True}


@app.post("/jobs/{job_id}/requeue")
def requeue_job(job_id: str):
    j = state.get_job(job_id)
    if j is None:
        raise _coded(404, "job_not_found")
    if j["status"] == "queued":
        return j  # already runnable — nothing to do (idempotent).
    if j["status"] in ("plotting", "planning", "paused", "awaiting_pen_change", "homing"):
        raise _coded(409, "cannot_requeue_running")
    state.update_job(job_id, status="queued", error=None, resume_path=None,
                     started_at=None, plotting_started_at=None,
                     stages=[], current_stage_index=0,
                     plan_status=None,
                     estimated_total_seconds=None,
                     distance_pendown_m=None,
                     distance_total_m=None,
                     pen_lifts=None)
    fresh = state.get_job(job_id)
    optimize_queue.enqueue_for_job(fresh)
    plan_queue.enqueue(fresh)
    return fresh


# Queue control ----------------------------------------------------------

@app.post("/queue/start")
def start_queue():
    plot_worker.start_queue()
    return {"ok": True}


@app.post("/queue/pause")
def pause_queue():
    job = state.active_job()
    if job is None or job["status"] != "plotting":
        raise _coded(409, "no_active_plotting_job")
    plot_worker.pause_active()
    return {"ok": True}


@app.post("/queue/pause-at-pen-up")
def pause_at_pen_up_queue():
    job = state.active_job()
    if job is None or job["status"] != "plotting":
        raise _coded(409, "no_active_plotting_job")
    try:
        plot_worker.pause_at_pen_lift_active()
    except RuntimeError as e:
        raise _worker_error(e)
    return {"ok": True}


@app.post("/queue/resume")
def resume_queue():
    try:
        plot_worker.resume_active()
    except RuntimeError as e:
        raise _worker_error(e)
    return {"ok": True}


@app.post("/queue/continue")
def continue_queue():
    try:
        plot_worker.continue_next()
    except RuntimeError as e:
        raise _worker_error(e)
    return {"ok": True}


@app.post("/queue/cancel")
def cancel_queue():
    try:
        plot_worker.cancel_active()
    except RuntimeError as e:
        raise _worker_error(e)
    return {"ok": True}


@app.post("/queue/calibrate")
def calibrate_queue():
    try:
        plot_worker.trigger_calibration()
    except RuntimeError as e:
        raise _worker_error(e)
    return {"ok": True}


# Settings ---------------------------------------------------------------

@app.get("/settings")
def get_settings():
    return config.snapshot()


@app.patch("/settings")
def patch_settings(req: SettingsUpdate):
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if not updates:
        raise _coded(400, "no_settings")
    config.update(**updates)
    return config.snapshot()


# System -----------------------------------------------------------------

@app.get("/version")
def get_version():
    return {"version": config.APP_VERSION}


# Updates ----------------------------------------------------------------

class UpdateSkip(BaseModel):
    version: str


@app.get("/update/status")
def update_status():
    return updates.get_status()


@app.post("/update/check")
def update_check():
    return updates.get_status(force=True)


@app.post("/update/skip")
def update_skip(req: UpdateSkip):
    # Only honour a skip for the version that's actually the latest, so a stale
    # tab can't suppress a release the user hasn't seen yet.
    status = updates.get_status()
    if req.version != status["latest"]:
        raise _coded(409, "update_skip_mismatch")
    updates.skip(req.version)
    return updates.get_status()


class UpdateApply(BaseModel):
    dry_run: bool = False
    force: bool = False


@app.get("/update/log")
def update_log():
    return {"log": updates.read_log()}


@app.post("/update/apply")
def update_apply(req: UpdateApply):
    # Re-check against the remote so we never kick off a pointless reset/install.
    status = updates.get_status(force=True)
    if not status["update_available"]:
        raise _coded(409, "update_already_current")
    # Don't launch a second updater on top of a running one (e.g. a double-click).
    if updates.update_in_progress():
        raise _coded(409, "update_in_progress")
    # Never restart the service mid-plot — it would wreck the running job.
    if state.snapshot()["status"] != "idle":
        raise _coded(409, "update_busy")
    # The wrapper does `git reset --hard`, which overwrites tracked files. Refuse
    # by default if any are locally modified, but let the UI confirm and retry
    # with force=true (the structured detail lets it show what will be lost).
    if not req.dry_run and not req.force:
        dirty = updates.dirty_files()
        if dirty:
            raise HTTPException(409, detail={
                "reason": "dirty",
                "message": "the app folder has local changes",
                "files": dirty,
            })
    updates.launch(dry_run=req.dry_run)
    return {
        "started": True,
        "dry_run": req.dry_run,
        "target": status["latest"],
    }


@app.post("/system/shutdown")
async def system_shutdown():
    # Delay the halt so the HTTP response flushes to the client first. Requires
    # the service user to have NOPASSWD sudo for /sbin/shutdown (set up by
    # install.sh) and the service's CapabilityBoundingSet to permit CAP_SETUID /
    # CAP_SETGID — otherwise sudo fails with "unable to change to root gid".
    async def _do():
        await asyncio.sleep(1.5)
        proc = await asyncio.create_subprocess_exec(
            "sudo", "-n", "/sbin/shutdown", "-h", "now",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.error("shutdown failed: rc=%d stderr=%s",
                      proc.returncode, stderr.decode(errors="replace").strip())
    asyncio.create_task(_do())
    return {"ok": True}


# WebSocket --------------------------------------------------------------

@app.websocket("/ws/state")
async def ws_state(ws: WebSocket):
    await ws.accept()
    state.add_client(ws)
    try:
        await ws.send_json({"type": "state", **state.snapshot()})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        state.remove_client(ws)


@app.websocket("/api/v1/ws/state")
async def api_ws_state(ws: WebSocket):
    # Depends() doesn't work on websocket routes — check the header by hand.
    # Also accept the key as `?api_key=...` for clients that can't easily set
    # custom headers on a WebSocket handshake (e.g. browser WebSocket API).
    api_key = ws.headers.get("x-api-key") or ws.query_params.get("api_key")
    if not api_key or api_key != config.API_KEY:
        # Calling close() before accept() rejects the upgrade — Starlette
        # responds to the handshake with HTTP 403 instead of completing it.
        await ws.close()
        return
    await ws.accept()
    state.add_client(ws)
    try:
        await ws.send_json({"type": "state", **state.snapshot()})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        state.remove_client(ws)
