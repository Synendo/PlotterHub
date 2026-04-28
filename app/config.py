"""Runtime configuration.

Loads from a JSON file on disk (writable by the service user) and falls back
to the PLOTTER_MODEL environment variable and hardcoded defaults. Edits from
the UI persist to the JSON file.

Settings are described once in the ``_SETTINGS`` table; load / snapshot /
update derive everything from it. Adding a new setting is a one-line change.
External callers keep accessing values as module-level uppercase attributes
(e.g. ``config.PLOTTER_MODEL``) — the table writes them via ``globals()``.
"""
import json
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

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


@dataclass(frozen=True)
class _Setting:
    name: str                                # snake_case key in config.json
    type: type                               # int, bool, float, or str
    default: Any
    validate: Callable[[Any], bool] | None = None  # returns True if value is acceptable


_SETTINGS: list[_Setting] = [
    _Setting("plotter_model", int, int(os.environ.get("PLOTTER_MODEL", "2")),
             lambda v: 1 <= v <= 8),
    _Setting("pause_between_layers_default", bool, True),
    _Setting("pause_after_job_default", bool, True),
    _Setting("delete_on_complete_default", bool, False),
    _Setting("speed_pendown_default", int, 25, lambda v: 1 <= v <= 110),
    _Setting("speed_penup_default", int, 75, lambda v: 1 <= v <= 110),
    _Setting("accel_default", int, 75, lambda v: 1 <= v <= 100),
    _Setting("optimize_default", bool, False),
    _Setting("optimize_tolerance_default_mm", float, 0.10,
             lambda v: 0.01 <= v <= 10.0),
    _Setting("optimize_linemerge_default", bool, True),
    _Setting("optimize_linesimplify_default", bool, True),
    _Setting("optimize_linesort_default", bool, True),
    _Setting("optimize_reloop_default", bool, True),
    _Setting("display_unit", str, None,
             lambda v: v in ("mm", "cm", "in")),
]

# Static API key for /api/v1/* routes — kept outside the schema because it
# auto-generates when missing rather than falling back to a hardcoded default.
API_KEY: str = ""


def _coerce(s: _Setting, raw: Any) -> Any | None:
    """Cast raw to the setting's declared type and run its validator. Returns
    None if the value is missing or invalid (caller decides what to do)."""
    if raw is None:
        return None
    try:
        if s.type is bool:
            v: Any = bool(raw)
        elif s.type is int:
            v = int(raw)
        elif s.type is float:
            v = float(raw)
        else:
            v = raw  # str
    except (TypeError, ValueError):
        log.warning("config: invalid %s in %s", s.name, CONFIG_PATH)
        return None
    if s.validate is not None and not s.validate(v):
        return None
    return v


def _set(s: _Setting, value: Any) -> None:
    globals()[s.name.upper()] = value


# Initialize module-level attributes from defaults so static imports see
# valid values before _load_from_disk runs.
for _s in _SETTINGS:
    _set(_s, _s.default)


def _load_from_disk() -> None:
    global API_KEY
    data: dict = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text())
        except Exception:
            log.exception("config: could not parse %s; using defaults", CONFIG_PATH)
            data = {}

    for s in _SETTINGS:
        if s.name not in data:
            continue
        raw = data[s.name]
        if raw is None:
            # Honour explicit null only for settings whose default is None
            # (currently just display_unit).
            if s.default is None:
                _set(s, None)
            continue
        v = _coerce(s, raw)
        if v is not None:
            _set(s, v)

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
    out: dict = {"api_key": API_KEY}
    for s in _SETTINGS:
        out[s.name] = globals()[s.name.upper()]
    return out


def update(**kwargs) -> None:
    for s in _SETTINGS:
        if s.name not in kwargs:
            continue
        v = _coerce(s, kwargs[s.name])
        if v is not None:
            _set(s, v)
    _save_to_disk()


_load_from_disk()
