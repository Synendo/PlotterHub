"""Runtime configuration.

Loads from a JSON file on disk (writable by the service user) and falls back
to the PLOTTER_MODEL environment variable and hardcoded defaults. Edits from
the UI persist to the JSON file.
"""
import json
import logging
import os
import secrets
from pathlib import Path

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
VERSION_PATH = BASE_DIR / "VERSION"


def _read_version() -> str:
    try:
        return VERSION_PATH.read_text().strip() or "unknown"
    except OSError:
        return "unknown"


APP_VERSION: str = _read_version()

PLOTTER_MODEL: int = int(os.environ.get("PLOTTER_MODEL", "2"))

# Static API key for /api/v1/* routes. Auto-generated on first run if missing
# from config.json; persisted thereafter so the macOS companion app sees a
# stable value across service restarts.
API_KEY: str = ""

# Defaults used for new jobs. The UI can override per-job; these are the
# starting values a freshly-dropped SVG picks up.
PAUSE_BETWEEN_LAYERS_DEFAULT: bool = True
PAUSE_AFTER_JOB_DEFAULT: bool = True
DELETE_ON_COMPLETE_DEFAULT: bool = False
SPEED_PENDOWN_DEFAULT: int = 25
SPEED_PENUP_DEFAULT: int = 75
ACCEL_DEFAULT: int = 75

# vpype-based SVG optimization. Disabled by default; tolerance is the value
# fed to linemerge + linesimplify. Step toggles let the user pick which vpype
# commands run as part of the pipeline.
OPTIMIZE_DEFAULT: bool = False
OPTIMIZE_TOLERANCE_DEFAULT_MM: float = 0.10
OPTIMIZE_LINEMERGE_DEFAULT: bool = True
OPTIMIZE_LINESIMPLIFY_DEFAULT: bool = True
OPTIMIZE_LINESORT_DEFAULT: bool = True
OPTIMIZE_RELOOP_DEFAULT: bool = True


def _coerce_int(data: dict, key: str) -> int | None:
    if key not in data:
        return None
    try:
        return int(data[key])
    except (TypeError, ValueError):
        log.warning("config: invalid %s in %s", key, CONFIG_PATH)
        return None


def _coerce_bool(data: dict, key: str) -> bool | None:
    if key not in data:
        return None
    return bool(data[key])


def _coerce_float(data: dict, key: str) -> float | None:
    if key not in data:
        return None
    try:
        return float(data[key])
    except (TypeError, ValueError):
        log.warning("config: invalid %s in %s", key, CONFIG_PATH)
        return None


def _load_from_disk() -> None:
    global PLOTTER_MODEL, SPEED_PENDOWN_DEFAULT, SPEED_PENUP_DEFAULT, ACCEL_DEFAULT
    global PAUSE_BETWEEN_LAYERS_DEFAULT, PAUSE_AFTER_JOB_DEFAULT, DELETE_ON_COMPLETE_DEFAULT
    global OPTIMIZE_DEFAULT, OPTIMIZE_TOLERANCE_DEFAULT_MM
    global OPTIMIZE_LINEMERGE_DEFAULT, OPTIMIZE_LINESIMPLIFY_DEFAULT
    global OPTIMIZE_LINESORT_DEFAULT, OPTIMIZE_RELOOP_DEFAULT
    global API_KEY
    data: dict = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text())
        except Exception:
            log.exception("config: could not parse %s; using defaults", CONFIG_PATH)
            data = {}
    v = _coerce_int(data, "plotter_model")
    if v is not None: PLOTTER_MODEL = v
    v = _coerce_int(data, "speed_pendown_default")
    if v is not None: SPEED_PENDOWN_DEFAULT = v
    v = _coerce_int(data, "speed_penup_default")
    if v is not None: SPEED_PENUP_DEFAULT = v
    v = _coerce_int(data, "accel_default")
    if v is not None: ACCEL_DEFAULT = v
    v = _coerce_bool(data, "pause_between_layers_default")
    if v is not None: PAUSE_BETWEEN_LAYERS_DEFAULT = v
    v = _coerce_bool(data, "pause_after_job_default")
    if v is not None: PAUSE_AFTER_JOB_DEFAULT = v
    v = _coerce_bool(data, "delete_on_complete_default")
    if v is not None: DELETE_ON_COMPLETE_DEFAULT = v
    v = _coerce_bool(data, "optimize_default")
    if v is not None: OPTIMIZE_DEFAULT = v
    v = _coerce_float(data, "optimize_tolerance_default_mm")
    if v is not None: OPTIMIZE_TOLERANCE_DEFAULT_MM = v
    v = _coerce_bool(data, "optimize_linemerge_default")
    if v is not None: OPTIMIZE_LINEMERGE_DEFAULT = v
    v = _coerce_bool(data, "optimize_linesimplify_default")
    if v is not None: OPTIMIZE_LINESIMPLIFY_DEFAULT = v
    v = _coerce_bool(data, "optimize_linesort_default")
    if v is not None: OPTIMIZE_LINESORT_DEFAULT = v
    v = _coerce_bool(data, "optimize_reloop_default")
    if v is not None: OPTIMIZE_RELOOP_DEFAULT = v
    api = data.get("api_key")
    if isinstance(api, str) and api.strip():
        API_KEY = api.strip()
    else:
        API_KEY = secrets.token_urlsafe(24)
        _save_to_disk()


def _save_to_disk() -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(snapshot(), indent=2) + "\n")
    except Exception:
        log.exception("config: failed to save %s", CONFIG_PATH)


def snapshot() -> dict:
    return {
        "plotter_model": PLOTTER_MODEL,
        "api_key": API_KEY,
        "pause_between_layers_default": PAUSE_BETWEEN_LAYERS_DEFAULT,
        "pause_after_job_default": PAUSE_AFTER_JOB_DEFAULT,
        "delete_on_complete_default": DELETE_ON_COMPLETE_DEFAULT,
        "speed_pendown_default": SPEED_PENDOWN_DEFAULT,
        "speed_penup_default": SPEED_PENUP_DEFAULT,
        "accel_default": ACCEL_DEFAULT,
        "optimize_default": OPTIMIZE_DEFAULT,
        "optimize_tolerance_default_mm": OPTIMIZE_TOLERANCE_DEFAULT_MM,
        "optimize_linemerge_default": OPTIMIZE_LINEMERGE_DEFAULT,
        "optimize_linesimplify_default": OPTIMIZE_LINESIMPLIFY_DEFAULT,
        "optimize_linesort_default": OPTIMIZE_LINESORT_DEFAULT,
        "optimize_reloop_default": OPTIMIZE_RELOOP_DEFAULT,
    }


def update(**kwargs) -> None:
    global PLOTTER_MODEL, SPEED_PENDOWN_DEFAULT, SPEED_PENUP_DEFAULT, ACCEL_DEFAULT
    global PAUSE_BETWEEN_LAYERS_DEFAULT, PAUSE_AFTER_JOB_DEFAULT, DELETE_ON_COMPLETE_DEFAULT
    global OPTIMIZE_DEFAULT, OPTIMIZE_TOLERANCE_DEFAULT_MM
    global OPTIMIZE_LINEMERGE_DEFAULT, OPTIMIZE_LINESIMPLIFY_DEFAULT
    global OPTIMIZE_LINESORT_DEFAULT, OPTIMIZE_RELOOP_DEFAULT
    if "plotter_model" in kwargs:
        PLOTTER_MODEL = int(kwargs["plotter_model"])
    if "pause_between_layers_default" in kwargs:
        PAUSE_BETWEEN_LAYERS_DEFAULT = bool(kwargs["pause_between_layers_default"])
    if "pause_after_job_default" in kwargs:
        PAUSE_AFTER_JOB_DEFAULT = bool(kwargs["pause_after_job_default"])
    if "delete_on_complete_default" in kwargs:
        DELETE_ON_COMPLETE_DEFAULT = bool(kwargs["delete_on_complete_default"])
    if "speed_pendown_default" in kwargs:
        SPEED_PENDOWN_DEFAULT = int(kwargs["speed_pendown_default"])
    if "speed_penup_default" in kwargs:
        SPEED_PENUP_DEFAULT = int(kwargs["speed_penup_default"])
    if "accel_default" in kwargs:
        ACCEL_DEFAULT = int(kwargs["accel_default"])
    if "optimize_default" in kwargs:
        OPTIMIZE_DEFAULT = bool(kwargs["optimize_default"])
    if "optimize_tolerance_default_mm" in kwargs:
        OPTIMIZE_TOLERANCE_DEFAULT_MM = float(kwargs["optimize_tolerance_default_mm"])
    if "optimize_linemerge_default" in kwargs:
        OPTIMIZE_LINEMERGE_DEFAULT = bool(kwargs["optimize_linemerge_default"])
    if "optimize_linesimplify_default" in kwargs:
        OPTIMIZE_LINESIMPLIFY_DEFAULT = bool(kwargs["optimize_linesimplify_default"])
    if "optimize_linesort_default" in kwargs:
        OPTIMIZE_LINESORT_DEFAULT = bool(kwargs["optimize_linesort_default"])
    if "optimize_reloop_default" in kwargs:
        OPTIMIZE_RELOOP_DEFAULT = bool(kwargs["optimize_reloop_default"])
    _save_to_disk()


_load_from_disk()
