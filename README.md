<p align="center">
  <img src="static/plotter_hub_logo.svg" alt="Plotter Hub" width="360">
</p>

A self-hosted plot server for the iDraw H SE A3 and AxiDraw-class pen plotters. Submit SVGs over the network and the Pi drives the plotter locally via the official AxiDraw Python API, so your workstation doesn't need to stay connected for the duration of the plot.

Open `http://plotterhub.local` (or whatever your Pi's hostname is) and you get a drag-and-drop UI with layer-by-layer plotting, pen-change pauses, paper-size presets, and a live pen-position cursor.

## Background

I didn't like that my iDraw H SE A3 plotter had to stay connected to my laptop to run a plot. Luckily it's compatible with the great [AxiDraw software](https://axidraw.com/), which can be installed on a Raspberry Pi — this repo is just a UI around [AxiDraw's Python library](https://axidraw.com/doc/py_api/).

I also had a look at [saxi](https://github.com/nornagon/saxi), but it didn't support the physical pause button on my iDraw. AxiDraw does recognize button presses, so Plotter Hub supports it: press the button once to pause, press it a second time to resume the plot. The same button also continues to the next layer when the plot is paused for a pen change.

**Disclaimer:** this code was completely created by [Claude Code](https://claude.com/claude-code) (Claude Opus 4.7-4.8, 1M-context).

## Features

**Plotting**

- Drag-and-drop SVG upload; Inkscape layers parsed and selectable
- Staged plotting: optional pause between layers for pen changes
- Paper presets (A0–A5, B0–B5, Letter, Legal, Ledger, ANSI C–E, Custom) + orientation
- 4-sided margins and fit-content-to-page
- Configurable pen-down / pen-up speed and acceleration
- Optional [vpype](https://vpype.readthedocs.io/) optimization (linemerge / linesimplify / linesort / reloop) before plotting; cached per job and reused across re-plots

**During the plot**

- Pre-plot estimate: time, pen-down distance, total distance, pen lifts
- Progress bar with remaining-time based on the estimate
- Live pen cursor on the preview (blue while drawing, grey while traveling)
- UI Pause / Resume / Cancel — cancel returns to origin via `res_home`
- Physical pause button toggles: press to pause, press again to resume

**Operational**

- Runs as a systemd service under the user who invoked `install.sh`
- In-app self-update: checks GitHub for new releases and updates with one click (Settings → About & Updates), guarded so it never runs mid-plot or over local changes
- Plot worker runs in a thread; preview runs in a subprocess (cancel-killable)
- In-memory preview cache — same SVG + same params skips the ~20–30s planning pass
- Graceful shutdown on service stop: pauses any in-flight plot so the pen is raised and the resume SVG is flushed

**API**

- HTTP API for companion apps and scripts under `/api/v1/*`, secured with an auto-generated `X-API-Key`
- See [API.md](API.md) for the endpoint reference and `multipart/form-data` schema

## Requirements

- Raspberry Pi Zero 2 W, 3B+, or newer running Raspberry Pi OS Trixie (Debian 13) or Bookworm (Debian 12)
- An iDraw H SE A3, AxiDraw, or compatible EBB-based plotter on USB

Tested on a Raspberry Pi 3 Model B and a Raspberry Pi Zero 2 W, both running Raspberry Pi OS Lite (64-bit) — a port of Debian Trixie with no desktop environment (released 2026-04-21).

**Hardware notes:** both the Zero 2 W and the 3B+ are capable hosts — which one fits best depends on how you plot. The **Zero 2 W** draws the least power and is well suited to an always-on box; it boots and optimizes/plans a job roughly 40% slower than a **3B+**, but that overhead is negligible next to the plotting time itself. If you plot a lot and want snappier setup and preview times, the 3B+ is the more comfortable choice. There's little reason to go beyond it to a Pi 4 or 5 when this is the only thing running — the workload never uses the extra performance.

`install.sh` checks these prerequisites and aborts with a hint if any are missing:

- Python ≥ 3.11 (default on Bookworm and newer)
- Service user is a member of the `dialout` group (for `/dev/ttyACM0`)
- `avahi-daemon` is running (warning only — needed for `.local` hostname)

### Dependencies installed by the script

**apt packages** (idempotent — apt skips anything already present):

- [`python3`](https://www.python.org/)
- [`python3-venv`](https://docs.python.org/3/library/venv.html)
- [`python3-pip`](https://pip.pypa.io/)

**Python packages**, pip-installed into a project-local `venv/`:

- [`fastapi`](https://fastapi.tiangolo.com/)
- [`uvicorn[standard]`](https://www.uvicorn.org/)
- [`python-multipart`](https://github.com/Kludex/python-multipart)
- [`pyaxidraw`](https://axidraw.com/doc/py_api/) (from the Evil Mad Scientist [AxiDraw API zip](https://cdn.evilmadscientist.com/dl/ad/public/AxiDraw_API.zip))
- [`vpype`](https://vpype.readthedocs.io/) — invoked as a subprocess for optional pre-plot optimization

**System files** (written / overwritten on every run):

- `/etc/systemd/system/plotterhub.service` — templated from `systemd/plotterhub.service` with the invoking user and the repo path
- `/etc/sudoers.d/plotterhub-shutdown` — grants the service user NOPASSWD on `/sbin/shutdown` so the UI's shutdown button works
- `/usr/local/sbin/plotterhub-update` — root-owned self-update helper invoked by the UI's "Update now" button (templated from `scripts/plotterhub-update.in`)
- `/etc/sudoers.d/plotterhub-update` — grants the service user NOPASSWD on just that helper

### Assumed already present on Raspberry Pi OS

The script relies on these but does not install them: `sudo`, `apt`, `systemctl`, `ss` (from `iproute2`), `install`, `visudo`, plus `git`, `runuser`, and `systemd-run` (used by the self-update path). They ship with any stock Raspberry Pi OS install.

## Install

On a clean Raspberry Pi, as whichever user you want the service to run as. From your workstation, ssh in (replace the hostname/username with your Pi's):

```bash
ssh plotter@plotterhub.local
```

Raspberry Pi OS Lite doesn't ship with git, so install it first if needed, then clone and run the installer:

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/Synendo/PlotterHub.git ~/PlotterHub
cd ~/PlotterHub
./install.sh
```

The script is idempotent — re-run after `git pull` to update dependencies and restart the service. Concretely:

- apt install is a no-op when packages are already current
- The `venv/` directory is only created if it doesn't exist; otherwise it's reused
- `pip install -r requirements.txt` skips packages whose spec is already satisfied
- The systemd unit and sudoers rule are re-templated and re-written every time
- `systemctl daemon-reload` / `enable` / `restart` are safe to repeat

If a previous install is already running, the script stops it first so the port probe doesn't see its own listener as a conflict, then binds port 80 if free, else port 8080.

The systemd unit runs the server as the user who invoked `install.sh`, from the directory where the repo was cloned — no specific username is required, and the clone path isn't constrained.

When the script finishes it prints the URL to open in your browser.

### Install options

```bash
# Unattended install (pipes sudo password):
SUDO_PW='your-password' ./install.sh

# Set a different plotter model at install (default is 2, AxiDraw SE/A3):
PLOTTER_MODEL=1 ./install.sh
```

After install, the plotter model can also be changed from the UI (gear icon → Settings) and is persisted to `config.json`.

### Network and access

Plotter Hub has no built-in login — anyone who can reach its web port can upload, plot, change settings, update, or shut down the Pi. That's intentional for a trusted home LAN (like a network printer's web page), but it means you should **keep it on your local network and not port-forward it to the internet**. For remote access, put it behind a VPN such as [Tailscale](https://tailscale.com/) or WireGuard rather than exposing it directly. The `X-API-Key` only guards the `/api/v1/*` endpoints and is itself readable on the LAN — it's a scripting convenience, not a security boundary.

## Updating

Plotter Hub can update itself from the web UI, or you can update manually over ssh. The UI path is the convenient one — no terminal needed.

### From the UI (recommended)

When a newer version is published on `main`, a banner appears at the top of the page — **Update available: `<current>` → `<latest>`** — with **Update now** and **Skip**. The same controls, plus the current version, an availability badge, and a **Check now** button, live under **Settings → About & Updates**.

- **Update now** pulls the latest version, re-runs `install.sh`, and restarts the service. The installer log streams live in a dialog and the page reconnects on its own once the new version is up (don't close the tab — it takes a minute or two).
- **Skip** hides the banner for that version; it comes back only when a *newer* version is released. You can still start the update later from Settings (the skip just suppresses the banner).
- The check queries the public GitHub repo over HTTPS (no credentials needed) and is cached for an hour, so a freshly published release may not show immediately — **Check now** forces a fresh check.

Updates are **refused while a plot is running** (wait until the queue is idle), and a second update can't start while one is already in progress. If the app folder has **local changes**, the update asks you to confirm before overwriting them — your settings, job queue, and uploads are always kept (they're gitignored, so `git reset` never touches them).

Under the hood: `install.sh` installs a small root-owned helper at `/usr/local/sbin/plotterhub-update` with a scoped NOPASSWD sudoers rule. When triggered it re-launches itself in a transient systemd unit so it survives the service restart, runs `git reset --hard` to the latest `main`, then re-runs `install.sh`. All output is written to `update.log`.

If an update doesn't come back up, the cause is usually in `update.log` (or `journalctl -u plotterhub -n 50`); ssh in and re-run `./install.sh` to recover.

### Manually over ssh

ssh to the Pi, pull the latest version of the repository and re-run the installer:

```bash
cd ~/PlotterHub
git pull
./install.sh
```

`install.sh` is idempotent, so re-running it is the upgrade path — `apt` skips satisfied packages, `pip` only installs requirements that changed, and the systemd unit is re-templated and restarted. Your `config.json`, `state.json`, and everything under `uploads/` is gitignored and preserved across upgrades; the job queue rehydrates on service start. Uploaded SVGs accumulate in `uploads/` over time — enable *Delete on complete* in Settings, or clear old files periodically, so a small SD card doesn't fill up.

Before upgrading (either way), it's cleanest to wait until the queue is idle (or the active job is `paused` / `awaiting_pen_change`). If you do upgrade mid-plot via the manual path, the graceful-shutdown handler pauses the active job and queue persistence restores it as a resumable paused job on the next start.

## Architecture

| Layer | What it is |
|---|---|
| Backend | Python 3.13, FastAPI, Uvicorn (uvloop + httptools) |
| Plotter control | `pyaxidraw` Python API (not the `axicli` CLI) |
| Optimization | `vpype` CLI invoked as a subprocess (cancel-killable) for optional pre-plot path optimization; per-job cache reused across re-plots |
| Frontend | Vanilla HTML + CSS + JavaScript, no build step |
| Transport | HTTP + WebSocket |
| State | In-memory, broadcast via `asyncio.Queue` |
| Process mgmt | systemd (`plotterhub.service`) |
| Persistence | Uploaded SVGs + resume SVGs on disk; `config.json` for plotter model; `state.json` for the job queue (so a paused plot survives a service restart) |

Key module layout:

```
app/
  main.py           # FastAPI routes, /upload, /plot, /pause, /resume, /continue,
                    # /cancel, /settings, /ws/state
  plot_worker.py    # plot + resume + homing worker thread,
                    # button-poll and position-poll threads, preview cache
  preview_runner.py # subprocess entry point for pyaxidraw preview mode
  svg_optimize.py   # vpype subprocess wrapper for optional pre-plot optimization
  svg_utils.py      # Inkscape-layer parsing, filter, paper transform
  state.py          # in-memory state + WebSocket broadcast
  config.py         # plotter model config, persisted to config.json
  updates.py        # self-update: remote version check + guarded apply
static/             # index.html, app.js, style.css
systemd/            # plotterhub.service (template)
scripts/            # plotterhub-update.in (self-update helper template)
install.sh          # idempotent installer
uploads/            # gitignored; uploaded SVGs and per-stage filtered / resume files
```

## Development

The local source of truth is on your workstation; deploy to the Pi via rsync:

```bash
# Replace <user>@<host> with your Pi's ssh target, and ~/PlotterHub with
# the path where you cloned the repo.
rsync -avz --exclude=.git --exclude=venv --exclude='uploads/*' \
  -e ssh ./ <user>@<host>.local:~/PlotterHub/
ssh <user>@<host>.local '~/PlotterHub/install.sh'
```

`install.sh` detects that dependencies are already installed and just restarts the service.

Never restart the service mid-plot — Python can't kill a thread, so a SIGTERM during `plot_run` would strand the pen. On modern installs the graceful-shutdown handler mitigates this by pausing first, but it's still better to wait until `status` is `idle`, `completed`, `failed`, or `cancelled`.

## Known limitations

- No live progress while `plot_run` is in its ~18s pre-motion setup phase (EBB version query, servo init, path planning) — pyaxidraw doesn't expose progress events until motion starts.

## License

Released under the MIT License — see [LICENSE](LICENSE). Built around the AxiDraw Python API from Evil Mad Scientist (GPL-2.0), which is installed as a runtime dependency (not bundled) so this project's license is unaffected.
1. 