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
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from . import config, plot_worker, state, svg_utils

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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    state.init(asyncio.get_running_loop())
    drain_task = asyncio.create_task(state.drain_events())
    try:
        yield
    finally:
        await asyncio.get_running_loop().run_in_executor(None, plot_worker.shutdown_gracefully)
        drain_task.cancel()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


# SVG storage -------------------------------------------------------------

@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    data = await file.read()
    # Sniff the bytes before writing: SVG starts with '<' (optionally after a
    # UTF-8 BOM and whitespace). Anything binary (JPG/PNG/PDF/...) fails fast
    # with a clean message rather than the lxml parse trace.
    head = data.lstrip(b"\xef\xbb\xbf").lstrip()
    if not head.startswith(b"<"):
        raise HTTPException(400, "Not an SVG file. Please drop a .svg.")
    svg_id = uuid.uuid4().hex[:8]
    path = UPLOAD_DIR / f"{svg_id}.svg"
    path.write_bytes(data)
    try:
        info = svg_utils.parse_layers(path)
    except Exception:
        path.unlink(missing_ok=True)
        raise HTTPException(400, "That doesn't look like a valid SVG.")
    return {"id": svg_id, "filename": file.filename or "upload.svg", **info}


@app.get("/svg/{svg_id}")
def get_svg(svg_id: str):
    path = UPLOAD_DIR / f"{svg_id}.svg"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(str(path), media_type="image/svg+xml")


# Jobs -------------------------------------------------------------------
#
# The optimize_* fields appear in three shapes:
#   - JobCreate (web POST):           required-with-defaults
#   - JobUpdate (web PATCH):          all-Optional, no defaults
#   - ApiJobMetadata (public POST):   all-Optional, server fills missing values
# Two mixins capture the variants so we don't repeat the field list three times.

class _OptimizeCreateFields(BaseModel):
    optimize: bool = False
    optimize_tolerance_mm: float = Field(0.10, ge=0.01, le=10.0)
    optimize_linemerge: bool = True
    optimize_linesimplify: bool = True
    optimize_linesort: bool = True
    optimize_reloop: bool = True


class _OptimizeOptionalFields(BaseModel):
    optimize: bool | None = None
    optimize_tolerance_mm: float | None = Field(None, ge=0.01, le=10.0)
    optimize_linemerge: bool | None = None
    optimize_linesimplify: bool | None = None
    optimize_linesort: bool | None = None
    optimize_reloop: bool | None = None


class JobCreate(_OptimizeCreateFields):
    svg_id: str
    filename: str = "upload.svg"
    name: str | None = None
    paper_size_name: str | None = None
    layer_selections: list[dict]
    pause_between_layers: bool = True
    pause_after_job: bool = True
    delete_on_complete: bool = False
    paper_w_mm: float
    paper_h_mm: float
    margin_top_mm: float = 0.0
    margin_right_mm: float = 0.0
    margin_bottom_mm: float = 0.0
    margin_left_mm: float = 0.0
    fit_content: bool = False
    transform_scale: float = Field(1.0, ge=0.01, le=5.0)
    transform_rotation_deg: float = Field(0.0, ge=0.0, le=360.0)
    transform_offset_x_mm: float = 0.0
    transform_offset_y_mm: float = 0.0
    speed_pendown: int = 25
    speed_penup: int = 75
    accel: int = 75


class MoveRequest(BaseModel):
    new_index: int = Field(..., ge=0)


class SettingsUpdate(BaseModel):
    plotter_model: int | None = Field(None, ge=1, le=8)
    pause_between_layers_default: bool | None = None
    pause_after_job_default: bool | None = None
    delete_on_complete_default: bool | None = None
    speed_pendown_default: int | None = Field(None, ge=1, le=110)
    speed_penup_default: int | None = Field(None, ge=1, le=110)
    accel_default: int | None = Field(None, ge=1, le=100)
    optimize_default: bool | None = None
    optimize_tolerance_default_mm: float | None = Field(None, ge=0.01, le=10.0)
    optimize_linemerge_default: bool | None = None
    optimize_linesimplify_default: bool | None = None
    optimize_linesort_default: bool | None = None
    optimize_reloop_default: bool | None = None
    display_unit: Literal["mm", "cm", "in"] | None = None


class JobUpdate(_OptimizeOptionalFields):
    layer_selections: list[dict] | None = None
    name: str | None = None
    paper_size_name: str | None = None
    pause_between_layers: bool | None = None
    pause_after_job: bool | None = None
    delete_on_complete: bool | None = None
    paper_w_mm: float | None = None
    paper_h_mm: float | None = None
    margin_top_mm: float | None = None
    margin_right_mm: float | None = None
    margin_bottom_mm: float | None = None
    margin_left_mm: float | None = None
    fit_content: bool | None = None
    transform_scale: float | None = Field(None, ge=0.01, le=5.0)
    transform_rotation_deg: float | None = Field(None, ge=0.0, le=360.0)
    transform_offset_x_mm: float | None = None
    transform_offset_y_mm: float | None = None
    speed_pendown: int | None = None
    speed_penup: int | None = None
    accel: int | None = None


@app.post("/jobs")
def create_job(req: JobCreate):
    path = UPLOAD_DIR / f"{req.svg_id}.svg"
    if not path.exists():
        raise HTTPException(404, "svg not found")
    if not any(s.get("selected", True) for s in (req.layer_selections or [])):
        raise HTTPException(400, "select at least one layer")
    job = state.add_job(req.model_dump())
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
        raise HTTPException(404)
    if j["status"] not in ("queued", "completed", "failed", "cancelled"):
        raise HTTPException(409, "cannot edit an active job")
    # exclude_unset so the client distinguishes "not sent" from "explicitly null"
    # — needed e.g. for paper_size_name which can be cleared back to None.
    updates = req.model_dump(exclude_unset=True)
    # Re-queue on edit so user can re-plot a finished/cancelled job without extra steps
    if j["status"] != "queued":
        updates["status"] = "queued"
        updates["error"] = None
    state.update_job(job_id, **updates)
    return state.get_job(job_id)


def delete_svg_files(svg_id: str | None) -> None:
    # Delete the source SVG and every derivative (preview / filtered / staged /
    # resume). svg_id is a uuid4 fragment, 1:1 with a job, so globbing on it
    # can't hit another job's files.
    if not svg_id:
        return
    for p in UPLOAD_DIR.glob(f"{svg_id}.*"):
        try:
            p.unlink()
        except OSError:
            log.exception("delete_svg_files: failed to unlink %s", p)


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str):
    j = state.get_job(job_id)
    if j is None:
        raise HTTPException(404)
    if j["status"] in ("plotting", "planning", "paused", "awaiting_pen_change", "homing"):
        raise HTTPException(409, "cannot remove an active job")
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


class ApiJobMetadata(_OptimizeOptionalFields):
    name: str | None = None
    paper_size: ApiPaperSize | None = None
    layers: list[ApiLayer] = Field(default_factory=list)
    pause_between_layers: bool | None = None
    pause_after_job: bool | None = None
    delete_on_complete: bool | None = None
    speed_pendown: int | None = Field(default=None, ge=1, le=110)
    speed_penup: int | None = Field(default=None, ge=1, le=110)
    accel: int | None = Field(default=None, ge=1, le=100)


def _resolve_paper(paper: ApiPaperSize | None,
                   svg_w_mm: float | None,
                   svg_h_mm: float | None) -> tuple[float, float, str | None]:
    """Return (paper_w_mm, paper_h_mm, display_name)."""
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

    paper_w_mm, paper_h_mm, paper_name = _resolve_paper(
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
        "paper_w_mm": paper_w_mm,
        "paper_h_mm": paper_h_mm,
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
        "accel": pick(meta.accel, config.ACCEL_DEFAULT),
        "optimize": pick(meta.optimize, config.OPTIMIZE_DEFAULT),
        "optimize_tolerance_mm": pick(meta.optimize_tolerance_mm, config.OPTIMIZE_TOLERANCE_DEFAULT_MM),
        "optimize_linemerge": pick(meta.optimize_linemerge, config.OPTIMIZE_LINEMERGE_DEFAULT),
        "optimize_linesimplify": pick(meta.optimize_linesimplify, config.OPTIMIZE_LINESIMPLIFY_DEFAULT),
        "optimize_linesort": pick(meta.optimize_linesort, config.OPTIMIZE_LINESORT_DEFAULT),
        "optimize_reloop": pick(meta.optimize_reloop, config.OPTIMIZE_RELOOP_DEFAULT),
    }
    return state.add_job(job_payload)


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
    return get_settings()


@app.patch("/api/v1/settings", dependencies=[Depends(require_api_key)])
def api_patch_settings(req: SettingsUpdate):
    return patch_settings(req)


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
        raise HTTPException(404)
    if j["status"] in ("plotting", "planning", "paused", "awaiting_pen_change", "homing"):
        raise HTTPException(409, "cannot move an active job")
    state.move_job(job_id, req.new_index)
    return {"ok": True}


@app.post("/jobs/{job_id}/requeue")
def requeue_job(job_id: str):
    j = state.get_job(job_id)
    if j is None:
        raise HTTPException(404)
    if j["status"] == "queued":
        return j  # already runnable — nothing to do (idempotent).
    if j["status"] in ("plotting", "planning", "paused", "awaiting_pen_change", "homing"):
        raise HTTPException(409, "Cannot re-queue a job that is still running")
    state.update_job(job_id, status="queued", error=None, resume_path=None,
                     started_at=None, plotting_started_at=None,
                     stages=[], current_stage_index=0)
    return state.get_job(job_id)


# Queue control ----------------------------------------------------------

@app.post("/queue/start")
def start_queue():
    plot_worker.start_queue()
    return {"ok": True}


@app.post("/queue/pause")
def pause_queue():
    job = state.active_job()
    if job is None or job["status"] != "plotting":
        raise HTTPException(409, "no active plotting job")
    plot_worker.pause_active()
    return {"ok": True}


@app.post("/queue/resume")
def resume_queue():
    try:
        plot_worker.resume_active()
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/queue/continue")
def continue_queue():
    try:
        plot_worker.continue_next()
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/queue/cancel")
def cancel_queue():
    try:
        plot_worker.cancel_active()
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


# Settings ---------------------------------------------------------------

@app.get("/settings")
def get_settings():
    return config.snapshot()


@app.patch("/settings")
def patch_settings(req: SettingsUpdate):
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, "no settings provided")
    config.update(**updates)
    return config.snapshot()


# System -----------------------------------------------------------------

@app.get("/version")
def get_version():
    return {"version": config.APP_VERSION}


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
