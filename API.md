# Plotter Hub API

This document describes the public HTTP API exposed by Plotter Hub for external clients (companion apps, CLI tools, scripts). All public endpoints live under the `/api/v1/` prefix and require an API key.

The web UI uses a separate, unauthenticated set of routes (e.g. `/jobs`, `/upload`, `/queue/*`). Those are an internal contract between the bundled HTML and the server — they may change without notice. **Build against `/api/v1/*` only.**

## Base URL

```
http://<your-pi-hostname>.local
```

Default port is 80. If port 80 is unavailable at install time, the installer falls back to 8080 (`http://<host>.local:8080`).

## Authentication

Every request to `/api/v1/*` must include the API key in an `X-API-Key` header.

```
X-API-Key: <your-key>
```

The key is generated automatically the first time the service starts and is persisted in `config.json`. To find it:

- **From the UI** — gear icon → Settings → "API Key" section (last item, collapsed by default). Use the *Copy* button.
- **From the host directly** — read the `api_key` field in `~/PlotterHub/config.json` on the Pi.

To rotate the key, edit `config.json` on the Pi (or delete the `api_key` line so a new key is generated on next start) and restart `plotterhub.service`.

### Errors

| Code | Meaning |
|---|---|
| `401 Unauthorized` | `X-API-Key` is missing or doesn't match. |
| `400 Bad Request` | Validation failure — invalid SVG, unknown paper preset, malformed metadata JSON. |
| `404 Not Found` | The job ID doesn't exist (for per-job endpoints). |
| `409 Conflict` | The action isn't valid in the current state (e.g. editing a plotting job — once those endpoints exist). |
| `503 Service Unavailable` | Server hasn't initialized the API key yet. |

Errors come back as `{"detail": "..."}`.

## Endpoints

### `POST /api/v1/jobs` — add a job

Adds a new job to the queue. Accepts `multipart/form-data` with two parts:

| Part | Type | Required | Notes |
|---|---|---|---|
| `file` | file | yes | The SVG. Must contain at least one Inkscape layer. |
| `metadata` | text (JSON) | no | Job metadata — see schema below. Omit entirely to use auto-detected defaults. |

#### Metadata schema

All fields are optional. Unspecified booleans, speeds, and `selected` flags fall back to server-side defaults (see `GET /api/v1/settings`).

```jsonc
{
  "name": "string",                   // Display name; replaces filename in the job card header.

  "paper_size": {
    "name": "string",                 // e.g. "A3", "Letter", or a custom label like "Square".
    "width": 200.0,                   // Numeric. Required if `height` is given (and vice versa).
    "height": 200.0,
    "unit": "mm" | "cm" | "in",       // Default "mm". Applies to width/height.
    "orientation": "portrait" | "landscape"  // Optional; swaps width/height if it disagrees.
  },

  // Job options — omit any field to inherit the corresponding server default.
  "pause_between_layers": true,       // Pause for pen change between selected layers (multi-layer only).
  "pause_after_job":      true,       // Pause after this job finishes (paper / pen swap before next).
  "delete_on_complete":   false,      // Auto-remove the job and its uploaded SVG once complete.

  // Plotter speed — omit any field to inherit the server default. Out-of-range values return 400.
  "speed_pendown": 30,                // 1–110
  "speed_penup":   80,                // 1–110
  "accel":         50,                // 1–100

  // SVG optimization (vpype). Omit any field to inherit the server default.
  // The optimized SVG is cached per job and reused across re-plots; changing
  // any field below invalidates the cache and re-runs the pipeline.
  "optimize":              true,      // Master toggle. When false the rest is ignored.
  "optimize_tolerance_mm": 0.10,      // 0.01–10.0; used by linemerge + linesimplify.
  "optimize_linemerge":    true,      // Stitch lines whose endpoints are within tolerance.
  "optimize_linesimplify": true,      // Reduce vertex count (Douglas-Peucker).
  "optimize_linesort":     true,      // Reorder lines to cut pen-up travel.
  "optimize_reloop":       true,      // Randomize closed-path start (cosmetic).

  "layers": [                         // Per-layer overrides keyed by SVG layer index.
    {
      "index": 0,                     // Required — the 0-based Inkscape layer index.
      "name": "string",               // Optional — overrides the embedded `inkscape:label`.
      "type": "pattern" | "text" | "svg" | "calibration",  // Optional — drives a small icon in the UI.
      "selected": false               // Optional, default true. `false` excludes the layer from the plot.
    }
  ]
}
```

##### Paper size resolution

- **Metadata omitted, or `paper_size` omitted** — paper dimensions are taken straight from the SVG's `width`/`height` attributes (parsed via the SVG's units / `viewBox`). Orientation is implicit in those dimensions: portrait if `width ≤ height`, landscape otherwise. If the resulting size matches a known preset (A0–A5, B0–B5, Letter, Legal, Ledger, ANSI C–E), the web UI labels the job accordingly; otherwise it's shown as a custom size with the raw mm.
- **`paper_size.name` set, `width`/`height` omitted** — `name` must match a known preset (see list below); preset dimensions are used.
- **`paper_size.width` and `paper_size.height` set** — those values are used after unit conversion. `name` is preserved as a display label only.
- **`paper_size.orientation`** — if given, the resolved dimensions are swapped if needed so `width >= height` (landscape) or `width <= height` (portrait).

Known presets: `A0`–`A5`, `B0`–`B5`, `Letter`, `Legal`, `Ledger`, `ANSI-C`, `ANSI-D`, `ANSI-E`. Any other `name` without explicit dimensions returns `400`.

##### Layer overrides

`layers[]` is keyed by `index` (matching the SVG's Inkscape layer order, 0-based). Layers not listed keep the SVG's embedded `inkscape:label`, have no `type`, and are **selected** by default. Listed layers can override `name`, `type`, and `selected` independently — supplying only `type` keeps the embedded label, and supplying only `selected: false` excludes the layer from the plot. If every layer is deselected the request returns `400`.

Layer types are decorative — the icon is shown in the layer list:

| Type | Meaning | Icon (web UI) |
|---|---|---|
| `pattern` | Generative / decorative pattern | waveform |
| `text` | Text rendered as paths | text bars |
| `svg` | A vector glyph or composed shape | triangle/circle/square |
| `calibration` | Registration / alignment marks | scope (crosshair-in-circle) |

#### Response

`200 OK`, JSON, the full job record:

```jsonc
{
  "id": "abc12345",                   // Job ID — use this for future per-job actions.
  "status": "queued",
  "created_at": 1777212168.88,
  "svg_id": "1ebd8a27",
  "filename": "APITest.svg",
  "name": "API Test (via GD Studio)",
  "paper_size_name": "A3",
  "layer_selections": [
    { "index": 0, "label": "Guilloché", "type": "pattern" },
    { "index": 1, "label": "Text",      "type": "text" },
    { "index": 2, "label": "Logo",      "type": "svg" }
  ],
  "paper_w_mm": 420.0,
  "paper_h_mm": 297.0,
  "pause_between_layers": true,       // From server-side defaults (Settings).
  "pause_after_job": true,
  "delete_on_complete": false,
  "speed_pendown": 25,
  "speed_penup": 75,
  "accel": 75
  // ... margins, transforms, timing fields, etc.
}
```

#### Example

```bash
curl -X POST http://plotterhub.local/api/v1/jobs \
  -H "X-API-Key: $PLOTTERHUB_API_KEY" \
  -F "file=@/path/to/drawing.svg" \
  -F 'metadata={"name":"Nightly run","paper_size":{"name":"A3","orientation":"landscape"},"layers":[{"index":0,"name":"Outline","type":"pattern"},{"index":1,"name":"Title","type":"text"}]}'
```

If your shell mangles the inline JSON (extra spaces, broken backslash continuations), put the JSON in a file and reference it:

```bash
curl -X POST http://plotterhub.local/api/v1/jobs \
  -H "X-API-Key: $PLOTTERHUB_API_KEY" \
  -F "file=@/path/to/drawing.svg" \
  -F "metadata=<./metadata.json"
```

### Queue control

All five endpoints take no body, return `{"ok": true}` on success, and respond `409 Conflict` (with a `detail` message) when the action isn't valid in the current state.

| Method | Path | What it does | 409 conditions |
|---|---|---|---|
| `POST` | `/api/v1/queue/plot` | Start the queue. Picks up the first queued job. | No queued job; queue already running. |
| `POST` | `/api/v1/queue/pause` | Pause the active plot. Pen is raised; resumable. | No actively-plotting job. |
| `POST` | `/api/v1/queue/resume` | Resume a paused plot. | No paused job; missing resume data. |
| `POST` | `/api/v1/queue/continue` | Advance past a pen-change pause, or accept the next job after `awaiting_next_job`. | Nothing waiting on a continue. |
| `POST` | `/api/v1/queue/cancel` | Cancel the active job (or the awaiting-next-job state). The plotter homes if it can. | No active job. |

#### Lifecycle cheat sheet

```
queued ──plot──► [optimizing] ──► planning ──► plotting ──pause──► paused ──resume──► plotting
                                                                                         │
                                                                                ──continue──► (next stage / next job)
                                                                                         │
                                                                                ──cancel──► homing ──► cancelled
```

`optimizing` is only entered when the job has `optimize: true` AND its cached
optimized SVG either doesn't exist or was produced with different parameters.
On subsequent re-plots of the same job the cache is reused and the worker
goes straight to `planning`.

#### Example

```bash
curl -X POST http://plotterhub.local/api/v1/queue/plot \
  -H "X-API-Key: $PLOTTERHUB_API_KEY"
```

### Per-job CRUD

All routes require `X-API-Key`. Job IDs are 8-hex-char strings returned from `POST /api/v1/jobs`. A `404 Not Found` is returned if the job ID doesn't exist.

#### `GET /api/v1/jobs` — list

Returns the full queue snapshot, mirroring what the WebSocket broadcasts:

```jsonc
{
  "queue":   [ /* array of job records, in queue order */ ],
  "active_id": "abc12345",          // null if no active job
  "awaiting_next_job": false,       // true between jobs when pause_after_job=true
  "status": "plotting"              // top-level worker status
}
```

#### `GET /api/v1/jobs/{id}` — get one

Returns the full job record (same shape as the `POST /api/v1/jobs` response).

#### `PATCH /api/v1/jobs/{id}` — edit

Body is JSON. All fields optional; only the fields you send are applied. To clear a nullable field (e.g. `paper_size_name`), send it explicitly as `null` — *omitted* fields are ignored, *null* fields are cleared.

Editable fields:

| Field | Type | Notes |
|---|---|---|
| `name` | string \| null | Display name override. |
| `paper_size_name` | string \| null | Display label for the paper size. |
| `paper_w_mm`, `paper_h_mm` | number | Paper dimensions in mm. |
| `margin_top_mm`, `margin_right_mm`, `margin_bottom_mm`, `margin_left_mm` | number | |
| `fit_content` | bool | Scale SVG to fit the printable area. |
| `transform_scale` | number | 0.01–5.0 |
| `transform_rotation_deg` | number | 0–360 |
| `transform_offset_x_mm`, `transform_offset_y_mm` | number | |
| `speed_pendown`, `speed_penup` | int | 1–110 |
| `accel` | int | 1–100 |
| `pause_between_layers`, `pause_after_job`, `delete_on_complete` | bool | |
| `optimize` | bool | Run the vpype optimization pipeline before planning. |
| `optimize_tolerance_mm` | number | 0.01–10.0 |
| `optimize_linemerge`, `optimize_linesimplify`, `optimize_linesort`, `optimize_reloop` | bool | Per-step toggles for the vpype pipeline. |
| `layer_selections` | array | `[{index, label, type?, selected?}]` — drives which layers plot. Entries with `selected: false` are kept in the list (so name/type metadata survives a toggle in the UI) but skipped when planning. |

Returns the full updated job record. **`409 Conflict`** if the job is currently active (`plotting`, `planning`, `paused`, `awaiting_pen_change`, `homing`).

A side-effect to be aware of: editing a job that's in a terminal state (`completed`, `failed`, `cancelled`) automatically transitions it back to `queued` so a re-plot doesn't need a separate `/requeue` call.

#### `POST /api/v1/jobs/{id}/move` — reorder

Body: `{"new_index": <0-based int>}`. Returns `{"ok": true}`. **`409 Conflict`** if the job is active.

#### `POST /api/v1/jobs/{id}/requeue` — re-queue

No body. Returns the updated job record. Idempotent on jobs that are already `queued` (returns the existing record). **`409 Conflict`** if the job is active.

#### `DELETE /api/v1/jobs/{id}` — remove

No body. Returns `{"ok": true}`. Removes the job from the queue **and deletes the uploaded SVG** plus all on-disk derivatives (preview / filtered / staged / resume). **`409 Conflict`** if the job is active.

### Live state stream

#### `WS /api/v1/ws/state`

Streams the same JSON messages the web UI consumes — every queue mutation, status change, and live pen-position tick.

**Authentication.** Either:

- `X-API-Key: <key>` header on the upgrade request (preferred), or
- `?api_key=<key>` query parameter (for clients like the browser `WebSocket` API that can't set custom headers on a handshake).

If the key is missing or wrong, the server **rejects the WebSocket upgrade with HTTP 403** — the connection is refused before any frames are exchanged.

#### Message shape

The first message after `accept()` is always a full `state` snapshot:

```jsonc
{
  "type": "state",
  "queue": [ /* job records */ ],
  "active_id": "abc12345",
  "awaiting_next_job": false,
  "status": "plotting",
  "error": null
}
```

Subsequent messages are either further `state` updates (whenever the queue or any job changes) or pen-position ticks:

```jsonc
{ "type": "position", "x_mm": 123.4, "y_mm": 56.7, "pen_down": true }
```

Clients should switch on `type` and treat unknown types as forward-compat noise.

#### Example (CLI)

`~/Desktop/Examples/plotterhub-api-test-ws.sh` — pure-stdlib Python wrapped in a shell launcher; streams every frame to stdout, pretty-printed. Honors `PLOTTERHUB_HOST` / `PLOTTERHUB_API_KEY` env overrides.

### Settings

Server-wide defaults that new jobs inherit (the same set the web UI exposes in its Settings modal).

#### `GET /api/v1/settings`

Returns the current snapshot:

```jsonc
{
  "plotter_model": 2,                       // 1–8 (see install.sh / Settings UI for the table)
  "api_key": "tnBvwhc8VMdew8hMjlp6GdpTtAxN_7pG",
  "pause_between_layers_default": true,
  "pause_after_job_default": true,
  "delete_on_complete_default": false,
  "speed_pendown_default": 25,              // 1–110
  "speed_penup_default": 75,                // 1–110
  "accel_default": 75,                      // 1–100
  "optimize_default": false,                // Run vpype before plotting on new jobs
  "optimize_tolerance_default_mm": 0.10,    // 0.01–10.0
  "optimize_linemerge_default": true,
  "optimize_linesimplify_default": true,
  "optimize_linesort_default": true,
  "optimize_reloop_default": true
}
```

#### `PATCH /api/v1/settings`

Body is sparse JSON — only the fields you send are applied. Returns the new snapshot.

| Field | Range / Type |
|---|---|
| `plotter_model` | int 1–8 |
| `pause_between_layers_default` | bool |
| `pause_after_job_default` | bool |
| `delete_on_complete_default` | bool |
| `speed_pendown_default` | int 1–110 |
| `speed_penup_default` | int 1–110 |
| `accel_default` | int 1–100 |
| `optimize_default` | bool |
| `optimize_tolerance_default_mm` | float 0.01–10.0 |
| `optimize_linemerge_default`, `optimize_linesimplify_default`, `optimize_linesort_default`, `optimize_reloop_default` | bool |

Out-of-range values return `400`. The `api_key` field is **not** writable through this endpoint — to rotate the key, edit `config.json` on the Pi and restart the service.

```bash
curl -X PATCH http://plotterhub.local/api/v1/settings \
  -H "X-API-Key: $PLOTTERHUB_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"speed_pendown_default": 30, "delete_on_complete_default": true}'
```

### System

#### `GET /api/v1/version`

Returns the running Plotter Hub version (read from the `VERSION` file at install time):

```json
{ "version": "1.0.2" }
```

Useful for an "About" surface in your client and for compatibility checks against future API revisions.

#### `POST /api/v1/system/shutdown`

Powers off the Raspberry Pi. The HTTP response is flushed first, then the system halts roughly 1.5 seconds later (the service unit is also stopped along with the OS). No body; returns `{"ok": true}` immediately on dispatch.

**Be careful** — there's no abort once the request is accepted. The web UI guards this behind a confirmation modal; an external client should do the same. Don't issue a shutdown while a plot is running: the plotter is left wherever the pen happens to be, and on next boot the queue rehydrates with a paused job whose pen is no longer in a known position.

```bash
curl -X POST http://plotterhub.local/api/v1/system/shutdown \
  -H "X-API-Key: $PLOTTERHUB_API_KEY"
```

## Roadmap

All currently planned endpoints are implemented. Future additions will be documented here as they land.
