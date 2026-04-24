# Plotter Hub

A self-hosted plot server for the iDraw H SE A3 and AxiDraw-class pen plotters. Submit SVGs over the network and the Pi drives the plotter locally via the official AxiDraw Python API, so your workstation doesn't need to stay connected for the duration of the plot.

Open `http://plotterhub.local/` (or whatever your Pi's hostname is) and you get a drag-and-drop UI with layer-by-layer plotting, pen-change pauses, paper-size presets, and a live pen-position cursor.

## Background

I didn't like that my iDraw H SE A3 plotter had to stay connected to my laptop to run a plot. Luckily it's compatible with the great [AxiDraw software](https://axidraw.com/), which can be installed on a Raspberry Pi — so this repo is just a UI around [AxiDraw's Python library](https://axidraw.com/doc/py_api/).

I also had a look at [saxi](https://github.com/nornagon/saxi), but it didn't support the physical pause button on my iDraw. AxiDraw does recognize button presses, so Plotter Hub supports it: press the button once to pause, press it a second time to resume the plot.

**Disclaimer:** this code was completely created by [Claude Code](https://claude.com/claude-code) (Claude Opus 4.7, 1M-context).

## Features

**Plotting**
- Drag-and-drop SVG upload; Inkscape layers parsed and selectable
- Staged plotting: optional pause between layers for pen changes
- Paper presets (A0–A5, B0–B5, Letter, Legal, Ledger, ANSI C–E, Custom) + orientation
- 4-sided margins and fit-content-to-page
- Configurable pen-down / pen-up speed and acceleration

**During the plot**
- Pre-plot estimate: time, pen-down distance, total distance, pen lifts
- Progress bar with remaining-time based on the estimate
- Live pen cursor on the preview (blue while drawing, grey while traveling)
- UI Pause / Resume / Cancel — cancel returns to origin via `res_home`
- Physical pause button toggles: press to pause, press again to resume

**Operational**
- Runs as a systemd service under the user who invoked `install.sh`
- Plot worker runs in a thread; preview runs in a subprocess (cancel-killable)
- In-memory preview cache — same SVG + same params skips the ~20–30s planning pass
- Graceful shutdown on service stop: pauses any in-flight plot so the pen is raised and the resume SVG is flushed

## Requirements

- Raspberry Pi 3B+ or newer running Raspberry Pi OS Trixie (Debian 13) or Bookworm (Debian 12)
- An iDraw H SE A3, AxiDraw, or compatible EBB-based plotter on USB

Tested on a Raspberry Pi 3 Model B running Raspberry Pi OS Lite (64-bit), a port of Debian Trixie with no desktop environment (released 2026-04-21).

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

**System files** (written / overwritten on every run):
- `/etc/systemd/system/plotterhub.service` — templated from `systemd/plotterhub.service` with the invoking user and the repo path
- `/etc/sudoers.d/plotterhub-shutdown` — grants the service user NOPASSWD on `/sbin/shutdown` so the UI's shutdown button works

### Assumed already present on Raspberry Pi OS

The script relies on these but does not install them: `sudo`, `apt`, `systemctl`, `ss` (from `iproute2`), `install`, `visudo`. They ship with any stock Raspberry Pi OS install.

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

## Updating

ssh to the Pi, pull the latest version of the repository and re-run the installer:

```bash
cd ~/PlotterHub
git pull
./install.sh
```

`install.sh` is idempotent, so re-running it is the upgrade path — `apt` skips satisfied packages, `pip` only installs requirements that changed, and the systemd unit is re-templated and restarted. Your `config.json`, `state.json`, and everything under `uploads/` is gitignored and preserved across upgrades; the job queue rehydrates on service start.

Before upgrading, it's cleanest to wait until the queue is idle (or the active job is `paused` / `awaiting_pen_change`). If you do upgrade mid-plot, the graceful-shutdown handler pauses the active job and queue persistence restores it as a resumable paused job on the next start.

## Architecture

| Layer | What it is |
|---|---|
| Backend | Python 3.13, FastAPI, Uvicorn (uvloop + httptools) |
| Plotter control | `pyaxidraw` Python API (not the `axicli` CLI) |
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
  svg_utils.py      # Inkscape-layer parsing, filter, paper transform
  state.py          # in-memory state + WebSocket broadcast
  config.py         # plotter model config, persisted to config.json
static/             # index.html, app.js, style.css
systemd/            # plotterhub.service (template)
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
