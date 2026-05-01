import hashlib
import json
import logging
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

from plotink import ebb_motion, ebb_serial
from pyaxidraw import axidraw

from . import config, state, svg_optimize, svg_utils

log = logging.getLogger(__name__)

STOPPED_COMPLETED = 0
STOPPED_PROGRAMMATIC_PAUSE = 1
STOPPED_BUTTON_PAUSE = 102
STOPPED_SOFTWARE_PAUSE = 103
_PAUSED_CODES = {STOPPED_PROGRAMMATIC_PAUSE, STOPPED_BUTTON_PAUSE, STOPPED_SOFTWARE_PAUSE}

_STOPPED_MESSAGES = {
    101: "Could not connect to the plotter. Check that it is powered on and plugged in.",
    104: "Lost connection to the plotter during the plot.",
}


def _format_stopped(code: int) -> str:
    return _STOPPED_MESSAGES.get(code, f"plot stopped unexpectedly (code {code})")


# Shared control state for the worker thread -------------------------------

_current_ad: axidraw.AxiDraw | None = None
_preview_proc: subprocess.Popen | None = None
_cancel_flag = threading.Event()           # cancel the active job
_continue_event = threading.Event()        # continue: pen change within a job, or next job
_calibrate_event = threading.Event()       # set alongside _continue_event to request a calibration plot from the awaiting_pen_change pause
_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()

_poll_thread: threading.Thread | None = None
_stop_polling = threading.Event()
_BUTTON_POLL_INTERVAL_S = 0.3

_position_thread: threading.Thread | None = None
_stop_position = threading.Event()
_POSITION_POLL_INTERVAL_S = 0.1

_preview_cache: "OrderedDict[str, dict]" = OrderedDict()
_PREVIEW_CACHE_MAX = 20

_UPLOAD_DIR_LAZY: Path | None = None


def _uploads() -> Path:
    global _UPLOAD_DIR_LAZY
    if _UPLOAD_DIR_LAZY is None:
        from .main import UPLOAD_DIR
        _UPLOAD_DIR_LAZY = UPLOAD_DIR
    return _UPLOAD_DIR_LAZY


# Preview cache ------------------------------------------------------------

def _preview_cache_key(svg_path: Path, layer_indices: list[int], job: dict) -> str:
    h = hashlib.sha1()
    try:
        h.update(svg_path.read_bytes())
    except Exception:
        h.update(str(svg_path).encode())
    payload = {
        "layers": sorted(layer_indices),
        "paper_w": job["paper_width_mm"],
        "paper_h": job["paper_height_mm"],
        "mt": job["margin_top_mm"],
        "mr": job["margin_right_mm"],
        "mb": job["margin_bottom_mm"],
        "ml": job["margin_left_mm"],
        "fit": job["fit_content"],
        "ts": job.get("transform_scale", 1.0),
        "tr": job.get("transform_rotation_deg", 0.0),
        "tx": job.get("transform_offset_x_mm", 0.0),
        "ty": job.get("transform_offset_y_mm", 0.0),
        "model": config.PLOTTER_MODEL,
        "sd": job["speed_pendown"],
        "su": job["speed_penup"],
        "acc": job["acceleration"],
    }
    h.update(json.dumps(payload, sort_keys=True).encode())
    return h.hexdigest()


def _preview_cache_get(key: str) -> dict | None:
    if key in _preview_cache:
        _preview_cache.move_to_end(key)
        return dict(_preview_cache[key])
    return None


def _preview_cache_put(key: str, value: dict) -> None:
    _preview_cache[key] = dict(value)
    _preview_cache.move_to_end(key)
    while len(_preview_cache) > _PREVIEW_CACHE_MAX:
        _preview_cache.popitem(last=False)


# Background polling -------------------------------------------------------

def _position_poll_loop() -> None:
    last = (None, None, None)
    while not _stop_position.is_set():
        ad = _current_ad
        if ad is not None and hasattr(ad, "pen") and hasattr(ad.pen, "phys"):
            try:
                x_in = ad.pen.phys.xpos
                y_in = ad.pen.phys.ypos
                z_up = getattr(ad.pen.phys, "z_up", None)
                if x_in is not None and y_in is not None:
                    pen_down = (z_up is False)
                    key = (x_in, y_in, pen_down)
                    if key != last:
                        state.emit_position(x_in * 25.4, y_in * 25.4, pen_down)
                        last = key
                if z_up is True and state.pause_at_pen_up_pending():
                    state.set_pause_at_pen_up_pending(False)
                    try:
                        ad.transmit_pause_request()
                    except Exception:
                        log.exception("pen-lift pause request failed")
            except Exception:
                pass
        _stop_position.wait(_POSITION_POLL_INTERVAL_S)


def _start_position_poll() -> None:
    global _position_thread
    _stop_position.clear()
    _position_thread = threading.Thread(target=_position_poll_loop, daemon=True)
    _position_thread.start()


def _stop_position_poll() -> None:
    global _position_thread
    _stop_position.set()
    t = _position_thread
    if t is not None and t.is_alive() and threading.current_thread() is not t:
        t.join(timeout=1.0)
    _position_thread = None
    state.set_pause_at_pen_up_pending(False)


_BUTTON_ACTIVE_STATUSES = ("paused", "awaiting_pen_change")


def _button_poll_loop(job_id: str) -> None:
    port = None
    pressed_status: str | None = None
    try:
        port = ebb_serial.openPort()
        if port is None:
            return
        try:
            ebb_motion.QueryPRGButton(port, verbose=False)
        except Exception:
            return
        while not _stop_polling.is_set():
            job = state.get_job(job_id)
            if job is None or job["status"] not in _BUTTON_ACTIVE_STATUSES:
                return
            try:
                response = ebb_motion.QueryPRGButton(port, verbose=False)
            except Exception:
                break
            if response and str(response).strip().startswith("1"):
                pressed_status = job["status"]
                break
            _stop_polling.wait(_BUTTON_POLL_INTERVAL_S)
    finally:
        if port is not None:
            try:
                ebb_serial.closePort(port)
            except Exception:
                pass

    if pressed_status is None:
        return
    job = state.get_job(job_id)
    if job is None or job["status"] != pressed_status:
        return
    if pressed_status == "paused":
        threading.Thread(target=_safe_resume, daemon=True).start()
    elif pressed_status == "awaiting_pen_change":
        threading.Thread(target=_safe_continue, daemon=True).start()


def _safe_resume() -> None:
    try:
        resume_active()
    except Exception:
        log.exception("auto-resume via button press failed")


def _safe_continue() -> None:
    try:
        continue_next()
    except Exception:
        log.exception("auto-continue via button press failed")


def _start_button_poll(job_id: str) -> None:
    global _poll_thread
    _stop_polling.clear()
    _poll_thread = threading.Thread(target=_button_poll_loop, args=(job_id,), daemon=True)
    _poll_thread.start()


def _stop_button_poll() -> None:
    global _poll_thread
    _stop_polling.set()
    t = _poll_thread
    if t is not None and t.is_alive() and threading.current_thread() is not t:
        t.join(timeout=2.0)
    _poll_thread = None


# pyaxidraw wrappers -------------------------------------------------------

def _run_stage(current_svg: Path, mode: str, job: dict) -> tuple[int, str]:
    global _current_ad
    ad = axidraw.AxiDraw()
    try:
        ad.plot_setup(str(current_svg))
        ad.options.mode = mode
        ad.options.model = config.PLOTTER_MODEL
        ad.options.speed_pendown = job["speed_pendown"]
        ad.options.speed_penup = job["speed_penup"]
        ad.options.accel = job["acceleration"]
        _current_ad = ad
        _start_position_poll()
        output_svg = ad.plot_run(output=True)
        return ad.plot_status.stopped, output_svg
    finally:
        _stop_position_poll()
        try:
            ad.disconnect()
        except Exception:
            pass
        _current_ad = None


def _run_preview(preview_svg_path: Path, job: dict) -> dict | None:
    global _preview_proc
    runner = Path(__file__).parent / "preview_runner.py"
    args = [
        sys.executable,
        str(runner),
        str(preview_svg_path),
        str(config.PLOTTER_MODEL),
        str(job["speed_pendown"]),
        str(job["speed_penup"]),
        str(job["acceleration"]),
    ]
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    _preview_proc = proc
    try:
        stdout, stderr = proc.communicate()
    finally:
        _preview_proc = None

    if proc.returncode != 0:
        log.warning("preview subprocess exited rc=%s: %s", proc.returncode, stderr.strip())
        return None
    for line in reversed(stdout.strip().split("\n")):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


# Public control API -------------------------------------------------------

def start_queue() -> None:
    """Kick off the worker if it isn't already running."""
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _cancel_flag.clear()
        _continue_event.clear()
        t = threading.Thread(target=_queue_loop, daemon=True)
        globals()["_worker_thread"] = t
        t.start()


def pause_active() -> None:
    state.set_pause_at_pen_up_pending(False)
    if _current_ad is not None:
        _current_ad.transmit_pause_request()


def pause_at_pen_lift_active() -> None:
    """Soft pause: defer until the next pen lift, so the pen doesn't stop
    mid-stroke (which can leave a dot with pump-action pens). If the pen is
    already up when called, pauses immediately."""
    ad = _current_ad
    if ad is None:
        raise RuntimeError("No active plot")
    z_up = None
    try:
        z_up = getattr(ad.pen.phys, "z_up", None)
    except Exception:
        z_up = None
    if z_up is True:
        ad.transmit_pause_request()
        return
    state.set_pause_at_pen_up_pending(True)


def resume_active() -> None:
    job = state.active_job()
    if job is None or job["status"] != "paused":
        raise RuntimeError("No paused job to resume")

    # Live scenario: worker thread is blocked in the mid-stage pause-wait loop.
    # Flip status to plotting and it unblocks.
    if _worker_thread is not None and _worker_thread.is_alive():
        if not job.get("resume_path"):
            raise RuntimeError("No resume data")
        _stop_button_poll()
        state.update_job(job["job_id"], status="plotting", plotting_started_at=time.time())
        return

    # Post-restart scenario: no worker thread exists. Start the queue loop —
    # its paused-first dispatch picks up this job and routes it to _resume_job,
    # which skips re-planning and jumps into the staged loop.
    start_queue()


def continue_next() -> None:
    """Continue: either next stage (pen-change pause) or next job (awaiting_next_job)."""
    if state.snapshot()["awaiting_next_job"]:
        state.set_awaiting_next_job(False)
        _continue_event.set()
        return
    job = state.active_job()
    if job and job["status"] == "awaiting_pen_change":
        _continue_event.set()
        return
    raise RuntimeError("Nothing to continue")


def trigger_calibration() -> None:
    """Run a one-shot plot of every layer with type='calibration', then return
    to the awaiting_pen_change pause. Only valid while the active job is
    paused at a pen change AND has at least one calibration layer."""
    job = state.active_job()
    if job is None or job["status"] != "awaiting_pen_change":
        raise RuntimeError("Calibration plot only available at a pen-change pause")
    has_cal = any(s.get("type") == "calibration"
                  for s in (job.get("layer_selections") or []))
    if not has_cal:
        raise RuntimeError("This job has no calibration layers")
    # Set _calibrate_event first; the wait loop checks it after waking on
    # _continue_event, so order matters.
    _calibrate_event.set()
    _continue_event.set()


def cancel_active() -> None:
    snap = state.snapshot()
    if snap["awaiting_next_job"]:
        state.set_awaiting_next_job(False)
        _cancel_flag.set()
        _continue_event.set()
        return
    job = state.active_job()
    if job is None:
        raise RuntimeError("No active job")

    # Post-restart: no worker thread to signal. Flip to cancelled directly;
    # the pen is wherever the user left it — they'll need to home manually.
    if _worker_thread is None or not _worker_thread.is_alive():
        state.update_job(job["job_id"], status="cancelled", resume_path=None)
        state.set_active(None)
        return

    st = job["status"]
    if st == "plotting":
        _cancel_flag.set()
        pause_active()
    elif st == "plotting_calibration":
        # Same shape as plotting: stop the AxiDraw mid-stroke; the calibration
        # phase sees _cancel_flag and homes via res_home before bailing.
        _cancel_flag.set()
        pause_active()
    elif st == "planning":
        _cancel_flag.set()
        if _preview_proc is not None:
            try:
                _preview_proc.terminate()
            except Exception:
                pass
    elif st == "optimizing":
        _cancel_flag.set()
        svg_optimize.cancel_current()
    elif st == "awaiting_pen_change":
        _cancel_flag.set()
        _continue_event.set()
    elif st == "paused":
        _stop_button_poll()
        _cancel_flag.set()
        # The pause-wait loop polls the job's status (not _cancel_flag), so
        # flipping to 'homing' is what actually unblocks it. The loop then
        # runs res_home with the saved resume_path and marks the job cancelled.
        state.update_job(job["job_id"], status="homing")
    else:
        raise RuntimeError(f"Cannot cancel job in status '{st}'")


def shutdown_gracefully(timeout_s: float = 30.0) -> None:
    snap = state.snapshot()
    job = state.active_job()
    if job and job["status"] in ("plotting", "homing") and _current_ad is not None:
        log.info("graceful shutdown: pausing active job %s", job["job_id"])
        try:
            _current_ad.transmit_pause_request()
        except Exception:
            log.exception("graceful shutdown: transmit_pause_request failed")
    _stop_button_poll()
    _stop_position_poll()
    t = _worker_thread
    if t is not None and t.is_alive():
        t.join(timeout=timeout_s)
        if t.is_alive():
            log.warning("graceful shutdown: worker thread did not exit within %ss", timeout_s)


# Queue loop ---------------------------------------------------------------

def _queue_loop() -> None:
    try:
        while True:
            if _cancel_flag.is_set():
                _cancel_flag.clear()
                return
            # Paused jobs take priority: they were interrupted mid-run by a
            # service restart and should be finished before any fresh queued
            # job starts.
            paused = state.next_paused_job()
            if paused is not None:
                state.set_active(paused["job_id"])
                _resume_job(paused["job_id"])
                state.set_active(None)
                if _cancel_flag.is_set():
                    _cancel_flag.clear()
                    continue
                # Fall through to between-jobs pause check below
                job = paused
            else:
                job = state.next_queued_job()
                if job is None:
                    return
                state.set_active(job["job_id"])
                _run_job(job["job_id"])
                state.set_active(None)

            if _cancel_flag.is_set():
                _cancel_flag.clear()
                continue  # loop; user may still have more queued

            # Between jobs: pause if the just-finished job asked for it and more queued
            if state.next_queued_job() is not None:
                last = state.get_job(job["job_id"])
                if last and last.get("pause_after_job", True):
                    state.set_awaiting_next_job(True)
                    _continue_event.wait()
                    _continue_event.clear()
                    state.set_awaiting_next_job(False)
                    if _cancel_flag.is_set():
                        _cancel_flag.clear()
                        continue
    except Exception:
        log.exception("queue loop crashed")
    finally:
        _stop_button_poll()


def _optimize_cache_key(job: dict) -> str:
    """Snapshot of the inputs that govern the .opt.svg contents.

    Stored on the job after a successful run so we re-optimize only when the
    user changes a setting that would actually change the output.
    """
    return "|".join([
        f"t={float(job.get('optimize_svg_tolerance_mm', 0.10)):.4f}",
        f"lm={int(bool(job.get('optimize_svg_linemerge', True)))}",
        f"ls={int(bool(job.get('optimize_svg_linesimplify', True)))}",
        f"so={int(bool(job.get('optimize_svg_linesort', True)))}",
        f"rl={int(bool(job.get('optimize_svg_reloop', True)))}",
    ])


def _effective_svg_path(job: dict) -> Path:
    """The SVG path to feed to filter_to_layers / transform_to_paper.

    If optimization is enabled and the cached .opt.svg is on disk, that's the
    one downstream uses. Otherwise we fall back to the raw upload.
    """
    src = _uploads() / f"{job['svg_id']}.svg"
    if not job.get("optimize_svg"):
        return src
    opt_path = src.with_name(f"{job['svg_id']}.opt.svg")
    return opt_path if opt_path.exists() else src


def _run_optimize_phase(job_id: str, src_path: Path, stages: list) -> Path | None:
    """Run vpype on ``src_path`` when the job has ``optimize_svg`` enabled.

    Return the SVG path the rest of the pipeline should use:
      - the cached/freshly-produced ``.opt.svg`` when optimization ran,
      - ``src_path`` unchanged when optimization is disabled,
      - ``None`` when optimization failed or the user cancelled — the caller
        should return immediately, the job has already been marked
        ``failed`` / ``cancelled``.

    Cache: keyed by ``_optimize_cache_key`` and stored on the job as
    ``optimized_with_key`` so re-plots with unchanged settings reuse the file.
    """
    job = state.get_job(job_id)
    if job is None or not job.get("optimize_svg"):
        return src_path

    opt_path = src_path.with_name(f"{job['svg_id']}.opt.svg")
    cache_key = _optimize_cache_key(job)
    if opt_path.exists() and job.get("optimized_with_key") == cache_key:
        return opt_path

    state.update_job(job_id,
                     status="optimizing",
                     started_at=time.time(),
                     plotting_started_at=None,
                     error=None,
                     stages=stages,
                     current_stage_index=0)

    cancelled = False
    try:
        svg_optimize.optimize_svg(
            src_path, opt_path,
            tolerance_mm=float(job.get("optimize_svg_tolerance_mm", 0.10)),
            linemerge=bool(job.get("optimize_svg_linemerge", True)),
            linesimplify=bool(job.get("optimize_svg_linesimplify", True)),
            linesort=bool(job.get("optimize_svg_linesort", True)),
            reloop=bool(job.get("optimize_svg_reloop", True)),
        )
    except svg_optimize.OptimizeError as e:
        # Cancel-via-terminate manifests as a non-zero rc — fall through to
        # the cancelled cleanup below instead of surfacing a "failed".
        if not _cancel_flag.is_set():
            state.update_job(job_id, status="failed",
                             error=f"Optimization failed: {e}")
            return None
        cancelled = True

    if cancelled or _cancel_flag.is_set():
        _cancel_flag.clear()
        try:
            opt_path.unlink(missing_ok=True)
        except OSError:
            pass
        state.update_job(job_id, status="cancelled")
        return None

    state.update_job(job_id, optimized_with_key=cache_key)
    return opt_path


def _run_calibration_phase(job_id: str, svg_path: Path) -> None:
    """Plot every type='calibration' layer of the job, regardless of the
    `selected` flag. Runs as a self-contained side plot from inside the
    awaiting_pen_change pause: no resume tracking, no stage advancement.

    Honours _cancel_flag — if the user hits cancel during the calibration
    plot, the AxiDraw is paused, we home with res_home, and return. The
    caller (the pause-wait loop in _run_staged_loop) then sees _cancel_flag
    and finalises the main job as cancelled.
    """
    job = state.get_job(job_id)
    if job is None:
        return
    cal_indices = [
        s["index"] for s in (job.get("layer_selections") or [])
        if s.get("type") == "calibration"
    ]
    if not cal_indices:
        return  # endpoint should have rejected — defensive

    state.update_job(job_id,
                     status="plotting_calibration",
                     plotting_started_at=time.time())

    filt = svg_path.with_name(f"{job['svg_id']}.cal.filt.svg")
    cal_svg = svg_path.with_name(f"{job['svg_id']}.cal.svg")

    output_svg = ""
    stopped = STOPPED_COMPLETED
    try:
        svg_utils.filter_to_layers(svg_path, cal_indices, filt)
        svg_utils.transform_to_paper(
            filt, cal_svg,
            job["paper_width_mm"], job["paper_height_mm"],
            job["margin_top_mm"], job["margin_right_mm"],
            job["margin_bottom_mm"], job["margin_left_mm"],
            job["fit_content"],
            transform_scale=job.get("transform_scale", 1.0),
            transform_rotation_deg=job.get("transform_rotation_deg", 0.0),
            transform_offset_x_mm=job.get("transform_offset_x_mm", 0.0),
            transform_offset_y_mm=job.get("transform_offset_y_mm", 0.0),
        )
        stopped, output_svg = _run_stage(cal_svg, "plot", job)
    except IndexError:
        log.warning("plotink IndexError during calibration plot")
        return
    except Exception:
        log.exception("calibration plot setup failed")
        return

    if stopped in _PAUSED_CODES and _cancel_flag.is_set():
        # User cancelled mid-calibration. Home from where we stopped, then
        # leave _cancel_flag set so the caller cancels the main job.
        resume_path = svg_path.with_name(f"{job['svg_id']}.cal.resume.svg")
        try:
            resume_path.write_text(output_svg, encoding="utf-8")
            _run_stage(resume_path, "res_home", job)
        except Exception:
            log.exception("calibration cancel: res_home failed")
        try:
            resume_path.unlink(missing_ok=True)
        except OSError:
            pass
        return

    if stopped != STOPPED_COMPLETED:
        # Treat anything other than success/cancel as an unhelpful warning —
        # the user is right there, can re-run calibration or continue.
        log.warning("calibration plot ended with stopped=%s", stopped)


def _resume_job(job_id: str) -> None:
    """Resume a job left in 'paused' by a service restart.

    Skips the planning/re-staging block of _run_job: the job's stages list and
    current_stage_index are already on disk. If a resume_path is set we
    continue from that partial SVG via res_plot; otherwise we're at a clean
    stage boundary (an awaiting_pen_change checkpoint) and the next stage is
    re-rendered fresh.
    """
    job = state.get_job(job_id)
    if job is None:
        return
    svg_path = _effective_svg_path(job)
    first_mode = "res_plot" if job.get("resume_path") else "plot"
    _run_staged_loop(job_id, svg_path, first_mode=first_mode)


def _run_job(job_id: str) -> None:
    """Optimize (optional) + plan + plot one job, possibly across multiple
    stages with pen-change pauses between."""
    job = state.get_job(job_id)
    if job is None:
        return

    # Build stages from the job's selections + pause_between_layers. Entries
    # with `selected: false` represent layers the user has toggled off in the
    # UI but whose metadata (name/type) we still want to preserve — skip them
    # when planning the plot.
    selections = [s for s in job["layer_selections"] if s.get("selected", True)]
    pause_between = job.get("pause_between_layers", True)
    if pause_between and len(selections) > 1:
        stages = [{"layer_indices": [s["index"]], "labels": [s["label"]], "status": "pending"}
                  for s in selections]
    else:
        stages = [{
            "layer_indices": [s["index"] for s in selections],
            "labels": [s["label"] for s in selections],
            "status": "pending",
        }]

    svg_path = _uploads() / f"{job['svg_id']}.svg"

    optimized = _run_optimize_phase(job_id, svg_path, stages)
    if optimized is None:
        return  # phase already marked the job as cancelled/failed
    svg_path = optimized

    # --- Planning (preview) -------------------------------------------------
    state.update_job(job_id,
                     stages=stages,
                     current_stage_index=0,
                     status="planning",
                     started_at=time.time(),
                     plotting_started_at=None,
                     resume_path=None,
                     error=None,
                     estimated_total_seconds=None,
                     distance_pendown_m=None,
                     distance_total_m=None,
                     pen_lifts=None)

    all_selected = [i for s in selections for i in [s["index"]]]
    cache_key = _preview_cache_key(svg_path, all_selected, job)
    cached = _preview_cache_get(cache_key)
    estimate: dict | None = None
    if cached is not None:
        log.info("preview cache hit for job %s", job_id)
        estimate = cached
    else:
        combined = svg_path.with_name(f"{job['svg_id']}.combined.filt.svg")
        svg_utils.filter_to_layers(svg_path, all_selected, combined)
        preview_svg = svg_path.with_name(f"{job['svg_id']}.preview.svg")
        svg_utils.transform_to_paper(
            combined, preview_svg,
            job["paper_width_mm"], job["paper_height_mm"],
            job["margin_top_mm"], job["margin_right_mm"],
            job["margin_bottom_mm"], job["margin_left_mm"],
            job["fit_content"],
            transform_scale=job.get("transform_scale", 1.0),
            transform_rotation_deg=job.get("transform_rotation_deg", 0.0),
            transform_offset_x_mm=job.get("transform_offset_x_mm", 0.0),
            transform_offset_y_mm=job.get("transform_offset_y_mm", 0.0),
        )
        estimate = _run_preview(preview_svg, job)
        if estimate:
            _preview_cache_put(cache_key, estimate)

    if _cancel_flag.is_set():
        state.update_job(job_id, status="cancelled")
        return

    if estimate:
        state.update_job(job_id, **estimate)

    # --- Stages -------------------------------------------------------------
    _run_staged_loop(job_id, svg_path, first_mode="plot")


def _run_staged_loop(job_id: str, svg_path: Path, first_mode: str) -> None:
    mode = first_mode
    while True:
        job = state.get_job(job_id)
        if job is None:
            return
        i = job["current_stage_index"]
        if i >= len(job["stages"]):
            state.update_job(job_id, status="completed", resume_path=None)
            return

        stage = job["stages"][i]

        if mode == "res_plot":
            current_svg = Path(job["resume_path"])
        else:
            filtered = svg_path.with_name(f"{job['svg_id']}.s{i}.filt.svg")
            svg_utils.filter_to_layers(svg_path, stage["layer_indices"], filtered)
            current_svg = svg_path.with_name(f"{job['svg_id']}.s{i}.svg")
            svg_utils.transform_to_paper(
                filtered, current_svg,
                job["paper_width_mm"], job["paper_height_mm"],
                job["margin_top_mm"], job["margin_right_mm"],
                job["margin_bottom_mm"], job["margin_left_mm"],
                job["fit_content"],
                transform_scale=job.get("transform_scale", 1.0),
                transform_rotation_deg=job.get("transform_rotation_deg", 0.0),
                transform_offset_x_mm=job.get("transform_offset_x_mm", 0.0),
                transform_offset_y_mm=job.get("transform_offset_y_mm", 0.0),
            )

        # Flag this stage as current on the job's stages list
        new_stages = [dict(s) for s in job["stages"]]
        new_stages[i] = dict(new_stages[i], status="current")
        state.update_job(job_id,
                         stages=new_stages,
                         status="plotting",
                         plotting_started_at=time.time())

        try:
            stopped, output_svg = _run_stage(current_svg, mode, job)
        except IndexError:
            log.warning("plotink IndexError; treating as plotter-not-ready")
            state.update_job(job_id, status="failed",
                             error="Plotter not ready. Wait a moment after power-on and try again.")
            return

        if stopped in _PAUSED_CODES:
            resume_path = svg_path.with_name(f"{job['svg_id']}.s{i}.resume.svg")
            resume_path.write_text(output_svg, encoding="utf-8")
            if _cancel_flag.is_set():
                _cancel_flag.clear()
                state.update_job(job_id, status="homing", resume_path=str(resume_path))
                try:
                    _run_stage(resume_path, "res_home", job)
                except Exception:
                    log.exception("res_home failed")
                state.update_job(job_id, status="cancelled", resume_path=None)
                return
            state.update_job(job_id, status="paused", resume_path=str(resume_path))
            _start_button_poll(job_id)
            # Wait for either resume or cancel via /resume or /cancel
            # We poll the status: when it flips to plotting, continue the loop.
            # When it flips to homing/cancelled, exit.
            while True:
                current = state.get_job(job_id)
                if current is None:
                    return
                st = current["status"]
                if st == "plotting":
                    _stop_button_poll()
                    mode = "res_plot"
                    break
                if st in ("cancelled", "homing"):
                    _stop_button_poll()
                    if st == "cancelled":
                        return
                    # homing
                    try:
                        _run_stage(Path(current["resume_path"]), "res_home", job)
                    except Exception:
                        log.exception("res_home failed")
                    state.update_job(job_id, status="cancelled", resume_path=None)
                    return
                time.sleep(0.1)
            continue

        if stopped != STOPPED_COMPLETED:
            state.update_job(job_id, status="failed", error=_format_stopped(stopped))
            return

        # Stage complete
        new_stages = [dict(s) for s in state.get_job(job_id)["stages"]]
        new_stages[i] = dict(new_stages[i], status="done")
        next_i = i + 1
        state.update_job(job_id, stages=new_stages, current_stage_index=next_i, resume_path=None)

        if next_i < len(new_stages):
            if job.get("pause_between_layers", True) and len(new_stages) > 1:
                state.update_job(job_id, status="awaiting_pen_change")
                _start_button_poll(job_id)
                while True:
                    _continue_event.wait()
                    _continue_event.clear()
                    if _cancel_flag.is_set():
                        _stop_button_poll()
                        _cancel_flag.clear()
                        state.update_job(job_id, status="cancelled")
                        return
                    if _calibrate_event.is_set():
                        _stop_button_poll()
                        _calibrate_event.clear()
                        _run_calibration_phase(job_id, svg_path)
                        if _cancel_flag.is_set():
                            _cancel_flag.clear()
                            state.update_job(job_id, status="cancelled",
                                             resume_path=None)
                            return
                        # Back to the pause point; loop and wait for the next
                        # continue / calibrate / cancel.
                        state.update_job(job_id, status="awaiting_pen_change")
                        _start_button_poll(job_id)
                        continue
                    # Plain continue → break out and run the next stage.
                    _stop_button_poll()
                    break
            mode = "plot"
            continue
        # No more stages
        state.update_job(job_id, status="completed", resume_path=None)
        if job.get("delete_on_complete", False):
            from .main import delete_svg_files
            svg_id = job.get("svg_id")
            state.remove_job(job_id)
            delete_svg_files(svg_id)
        return


# Cancel-aware cancel from the 'homing' status:
# We piggyback on the _cancel_flag path above. If user clicks cancel while
# paused, the cancel branch inside the pause-wait converts the paused job to
# homing, runs res_home, then cancelled. The worker never blocks uninterruptibly.
