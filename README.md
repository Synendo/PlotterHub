# Plotter Hub

A self-hosted plot server for the iDraw H SE A3 and AxiDraw-class pen plotters. Submit SVGs over the network and the Pi drives the plotter locally via the official AxiDraw Python API, so your workstation doesn't need to stay connected for the duration of the plot.

Open `http://plotterhub.local/` (or whatever your Pi's hostname is) and you get a drag-and-drop UI with layer-by-layer plotting, pen-change pauses, paper-size presets, and a live pen-position cursor.

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
- Runs as a systemd service under the `plotter` user
- Plot worker runs in a thread; preview runs in a subprocess (cancel-killable)
- In-memory preview cache — same SVG + same params skips the ~20–30s planning pass
- Graceful shutdown on service stop: pauses any in-flight plot so the pen is raised and the resume SVG is flushed

## Requirements

- Raspberry Pi 3B+ or newer running Raspberry Pi OS Trixie (Debian 13)
- Python 3.11+
- An iDraw H SE A3, AxiDraw, or compatible EBB-based plotter on USB
- Service user must be a member of the `dialout` group (for `/dev/ttyACM0`)

## Install

On a clean Raspberry Pi, as whichever user you want the service to run as:

```bash
git clone <this-repo> ~/plotterhub
cd ~/plotterhub
./install.sh
```

The script is idempotent — re-run after `git pull` to update dependencies and restart the service. It will:

1. Check Python ≥ 3.11, confirm the current user is in `dialout`, verify avahi-daemon is running
2. Install `python3`, `python3-venv`, `python3-pip` via apt
3. Create a venv and install Python dependencies including `pyaxidraw`
4. Install the systemd unit — templating the invoking user and the clone path into it — and bind port 80 if free, else port 8080
5. Start the service

The systemd unit runs the server as the user who invoked `install.sh`, from the directory where the repo was cloned — no need to be `plotter`, and the clone path isn't constrained to `~/plotterhub`.

When the script finishes it prints the URL to open in your browser.

### Install options

```bash
# Unattended install (pipes sudo password):
SUDO_PW='your-password' ./install.sh

# Set a different plotter model at install (default is 2, AxiDraw SE/A3):
PLOTTER_MODEL=1 ./install.sh
```

After install, the plotter model can also be changed from the UI (gear icon → Settings) and is persisted to `config.json`.

## Architecture

| Layer | What it is |
|---|---|
| Backend | Python 3.13, FastAPI, Uvicorn (uvloop + httptools) |
| Plotter control | `pyaxidraw` Python API (not the `axicli` CLI) |
| Frontend | Vanilla HTML + CSS + JavaScript, no build step |
| Transport | HTTP + WebSocket |
| State | In-memory, broadcast via `asyncio.Queue` |
| Process mgmt | systemd (`plotterhub.service`) |
| Persistence | Uploaded SVGs + resume SVGs on disk; `config.json` for plotter model |

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
rsync -avz --exclude=.git --exclude=venv --exclude='uploads/*' \
  -e ssh ./ plotter@plotterhub.local:/home/plotter/plotterhub/
ssh plotter@plotterhub.local './plotterhub/install.sh'
```

`install.sh` detects that dependencies are already installed and just restarts the service.

Never restart the service mid-plot — Python can't kill a thread, so a SIGTERM during `plot_run` would strand the pen. On modern installs the graceful-shutdown handler mitigates this by pausing first, but it's still better to wait until `status` is `idle`, `completed`, `failed`, or `cancelled`.

## Known limitations

- In-memory state: a paused plot is preserved on disk (resume SVG) but the UI on a fresh page load after a restart won't offer "Resume previous job". Future work.
- No live progress while `plot_run` is in its ~18s pre-motion setup phase (EBB version query, servo init, path planning) — pyaxidraw doesn't expose progress events until motion starts.

## License

Released under the MIT License — see [LICENSE](LICENSE). Built around the AxiDraw Python API from Evil Mad Scientist (GPL-2.0), which is installed as a runtime dependency (not bundled) so this project's license is unaffected.
