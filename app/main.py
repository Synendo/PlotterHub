import asyncio
import logging
import subprocess
import uuid

log = logging.getLogger(__name__)
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, plot_worker, state, svg_utils

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
STATIC_DIR = BASE_DIR / "static"


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
    svg_id = uuid.uuid4().hex[:8]
    path = UPLOAD_DIR / f"{svg_id}.svg"
    path.write_bytes(await file.read())
    try:
        info = svg_utils.parse_layers(path)
    except Exception as e:
        path.unlink(missing_ok=True)
        raise HTTPException(400, f"invalid SVG: {e}")
    return {"id": svg_id, "filename": file.filename or "upload.svg", **info}


@app.get("/svg/{svg_id}")
def get_svg(svg_id: str):
    path = UPLOAD_DIR / f"{svg_id}.svg"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(str(path), media_type="image/svg+xml")


# Jobs -------------------------------------------------------------------

class JobCreate(BaseModel):
    svg_id: str
    filename: str = "upload.svg"
    layer_selections: list[dict]
    pause_between_layers: bool = True
    pause_after_job: bool = True
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


class JobUpdate(BaseModel):
    layer_selections: list[dict] | None = None
    pause_between_layers: bool | None = None
    pause_after_job: bool | None = None
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
    if not req.layer_selections:
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
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    # Re-queue on edit so user can re-plot a finished/cancelled job without extra steps
    if j["status"] != "queued":
        updates["status"] = "queued"
        updates["error"] = None
    state.update_job(job_id, **updates)
    return state.get_job(job_id)


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str):
    j = state.get_job(job_id)
    if j is None:
        raise HTTPException(404)
    if j["status"] in ("plotting", "planning", "paused", "awaiting_pen_change", "homing"):
        raise HTTPException(409, "cannot remove an active job")
    state.remove_job(job_id)
    return {"ok": True}


class MoveRequest(BaseModel):
    new_index: int = Field(..., ge=0)


@app.post("/jobs/{job_id}/move")
def move_job(job_id: str, req: MoveRequest):
    j = state.get_job(job_id)
    if j is None:
        raise HTTPException(404)
    if j["status"] in ("plotting", "planning", "paused", "awaiting_pen_change", "homing"):
        raise HTTPException(409, "cannot move an active job")
    state.move_job(job_id, req.new_index)
    return {"ok": True}


class RequeueRequest(BaseModel):
    pass


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


class SettingsUpdate(BaseModel):
    plotter_model: int | None = Field(None, ge=1, le=8)
    speed_pendown_default: int | None = Field(None, ge=1, le=110)
    speed_penup_default: int | None = Field(None, ge=1, le=110)
    accel_default: int | None = Field(None, ge=1, le=100)


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
