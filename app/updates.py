"""Update detection against the public GitHub repo.

Reads the ``VERSION`` file on ``origin/main`` over HTTPS and compares it with
the locally installed version. Detection deliberately uses the same ``git
fetch`` path the apply step will use: if we can't reach the repo we couldn't
update anyway, so it's honest to surface the same failure here.

The fetch targets the canonical HTTPS URL explicitly rather than whatever the
local ``origin`` happens to be — the repo is public, so HTTPS needs no
credentials, and this works even on a checkout whose ``origin`` is an SSH URL
with no key configured. ``git fetch`` only writes ``.git``/``FETCH_HEAD``; the
working tree is never touched.
"""
import logging
import subprocess
import time

from . import config

log = logging.getLogger(__name__)

REPO_HTTPS_URL = "https://github.com/Synendo/PlotterHub.git"
REMOTE_BRANCH = "main"
CACHE_TTL_S = 3600  # don't hammer GitHub on every page poll

# Root-owned wrapper installed by install.sh; the service user may run exactly
# this path (and `--dry-run`) via passwordless sudo.
WRAPPER_PATH = "/usr/local/sbin/plotterhub-update"
UPDATE_LOG = config.BASE_DIR / "update.log"

_cache_latest: str | None = None
_cache_error: bool = False
_cache_at: float = 0.0


def _parse(v: str | None) -> tuple[int, ...] | None:
    if not v:
        return None
    try:
        return tuple(int(p) for p in v.strip().split("."))
    except ValueError:
        return None


def semver_gt(a: str | None, b: str | None) -> bool:
    """True if version ``a`` is strictly newer than ``b``. Numeric, not string,
    comparison (so 1.1.10 > 1.1.4). Unknown/unparsable versions sort lowest and
    therefore never present as an available update."""
    pa, pb = _parse(a), _parse(b)
    if pa is None:
        return False
    if pb is None:
        return True
    return pa > pb


def fetch_remote_version(timeout: float = 8.0) -> str | None:
    """Return the VERSION file content on origin/main, or None on any error."""
    base = str(config.BASE_DIR)
    try:
        subprocess.run(
            ["git", "-C", base, "fetch", "--quiet",
             REPO_HTTPS_URL, REMOTE_BRANCH],
            check=True, capture_output=True, timeout=timeout,
        )
        out = subprocess.run(
            ["git", "-C", base, "show", "FETCH_HEAD:VERSION"],
            check=True, capture_output=True, text=True, timeout=timeout,
        )
        return out.stdout.strip() or None
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("update check failed: %s", e)
        return None


def _git(*args: str, timeout: float = 10.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(config.BASE_DIR), *args],
        capture_output=True, text=True, timeout=timeout,
    )


def get_status(force: bool = False) -> dict:
    """Cached update status. ``force=True`` (the "Check now" button) bypasses
    the TTL and re-fetches immediately."""
    global _cache_latest, _cache_error, _cache_at
    now = time.time()
    if force or _cache_at == 0.0 or (now - _cache_at) >= CACHE_TTL_S:
        latest = fetch_remote_version()
        _cache_error = latest is None
        # Keep a previously known version on a transient failure so the banner
        # doesn't flicker away when the network blips.
        if latest is not None:
            _cache_latest = latest
        _cache_at = now

    current = config.APP_VERSION
    latest = _cache_latest
    return {
        "current": current,
        "latest": latest,
        "update_available": semver_gt(latest, current),
        "skipped": bool(latest) and latest == config.SKIPPED_VERSION,
        "checked_at": _cache_at,
        "error": _cache_error,
    }


def skip(version: str) -> None:
    """Remember that the user dismissed this version. The banner reappears only
    when a newer remote version shows up."""
    config.update(skipped_version=version)


def dirty_files() -> list[str]:
    """Tracked files with local modifications (staged or unstaged). Untracked
    files are deliberately ignored — `git reset --hard` doesn't touch them, so
    they never block an update. Returns ``["<unknown>"]`` if git can't be
    queried, so the caller still refuses rather than blindly clobbering. The
    list is shown to the user before they confirm an overwrite."""
    try:
        out = _git("status", "--porcelain", "--untracked-files=no")
    except (subprocess.SubprocessError, OSError):
        return ["<unknown>"]
    if out.returncode != 0:
        return ["<unknown>"]
    files = []
    for line in out.stdout.splitlines():
        # porcelain format is "XY <path>"; drop the 2 status chars + space.
        path = line[3:].strip()
        if path:
            files.append(path)
    return files


def read_log(max_bytes: int = 16384) -> str:
    """Tail of the update log the wrapper writes; polled by the UI."""
    try:
        return UPDATE_LOG.read_text()[-max_bytes:]
    except OSError:
        return ""


def launch(dry_run: bool = False) -> None:
    """Fire-and-forget the update wrapper. It re-execs itself into a transient
    systemd unit, so this child exits immediately and the work survives the
    service restart."""
    args = ["sudo", "-n", WRAPPER_PATH]
    if dry_run:
        args.append("--dry-run")
    subprocess.Popen(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
