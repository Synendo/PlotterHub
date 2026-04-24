"""Runtime configuration.

Loads from a JSON file on disk (writable by the service user) and falls back
to the PLOTTER_MODEL environment variable and hardcoded defaults. Edits from
the UI persist to the JSON file.
"""
import json
import logging
import os
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

# Defaults used for new jobs. The UI can override per-job; these are the
# starting values a freshly-dropped SVG picks up.
SPEED_PENDOWN_DEFAULT: int = 25
SPEED_PENUP_DEFAULT: int = 75
ACCEL_DEFAULT: int = 75


def _coerce_int(data: dict, key: str) -> int | None:
    if key not in data:
        return None
    try:
        return int(data[key])
    except (TypeError, ValueError):
        log.warning("config: invalid %s in %s", key, CONFIG_PATH)
        return None


def _load_from_disk() -> None:
    global PLOTTER_MODEL, SPEED_PENDOWN_DEFAULT, SPEED_PENUP_DEFAULT, ACCEL_DEFAULT
    if not CONFIG_PATH.exists():
        return
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except Exception:
        log.exception("config: could not parse %s; using defaults", CONFIG_PATH)
        return
    v = _coerce_int(data, "plotter_model")
    if v is not None: PLOTTER_MODEL = v
    v = _coerce_int(data, "speed_pendown_default")
    if v is not None: SPEED_PENDOWN_DEFAULT = v
    v = _coerce_int(data, "speed_penup_default")
    if v is not None: SPEED_PENUP_DEFAULT = v
    v = _coerce_int(data, "accel_default")
    if v is not None: ACCEL_DEFAULT = v


def _save_to_disk() -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(snapshot(), indent=2) + "\n")
    except Exception:
        log.exception("config: failed to save %s", CONFIG_PATH)


def snapshot() -> dict:
    return {
        "plotter_model": PLOTTER_MODEL,
        "speed_pendown_default": SPEED_PENDOWN_DEFAULT,
        "speed_penup_default": SPEED_PENUP_DEFAULT,
        "accel_default": ACCEL_DEFAULT,
    }


def update(**kwargs) -> None:
    global PLOTTER_MODEL, SPEED_PENDOWN_DEFAULT, SPEED_PENUP_DEFAULT, ACCEL_DEFAULT
    if "plotter_model" in kwargs:
        PLOTTER_MODEL = int(kwargs["plotter_model"])
    if "speed_pendown_default" in kwargs:
        SPEED_PENDOWN_DEFAULT = int(kwargs["speed_pendown_default"])
    if "speed_penup_default" in kwargs:
        SPEED_PENUP_DEFAULT = int(kwargs["speed_penup_default"])
    if "accel_default" in kwargs:
        ACCEL_DEFAULT = int(kwargs["accel_default"])
    _save_to_disk()


_load_from_disk()
