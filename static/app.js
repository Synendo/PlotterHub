const $ = (id) => document.getElementById(id);

const statusEl = $("status");
const dropZone = $("drop-zone");
const fileInput = $("file-input");
const uploadError = $("upload-error");
const queueList = $("queue-list");
const queueEmpty = $("queue-empty");
const queueControls = $("queue-controls");
const topMessage = $("top-message");
const plotBtn = $("plot-btn");
const pauseBtn = $("pause-btn");
const resumeBtn = $("resume-btn");
const continueBtn = $("continue-btn");
const cancelBtn = $("cancel-btn");
const jobCardTemplate = $("job-card-template");
const queueProgress = $("queue-progress");

const STATUS_LABELS = {
  idle: "Idle",
  queued: "Queued",
  planning: "Planning",
  plotting: "Plotting",
  paused: "Paused",
  awaiting_pen_change: "Awaiting pen change",
  awaiting_next_job: "Awaiting next job",
  homing: "Homing",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};
function statusLabel(key) {
  return STATUS_LABELS[key] || key;
}

let appSettings = {
  plotter_model: 2,
  speed_pendown_default: 25,
  speed_penup_default: 75,
  accel_default: 75,
};

// Paper size database (portrait dims). Landscape swaps them.
const PAPER_SIZES = {
  A0: { w: 841, h: 1189 },
  A1: { w: 594, h: 841 },
  A2: { w: 420, h: 594 },
  A3: { w: 297, h: 420 },
  A4: { w: 210, h: 297 },
  A5: { w: 148, h: 210 },
  B0: { w: 1000, h: 1414 },
  B1: { w: 707, h: 1000 },
  B2: { w: 500, h: 707 },
  B3: { w: 353, h: 500 },
  B4: { w: 250, h: 353 },
  B5: { w: 176, h: 250 },
  Letter: { w: 216, h: 279 },
  Legal: { w: 216, h: 356 },
  Ledger: { w: 279, h: 432 },
  "ANSI-C": { w: 432, h: 559 },
  "ANSI-D": { w: 559, h: 864 },
  "ANSI-E": { w: 864, h: 1118 },
};

// Runtime state mirrored from the server.
let serverState = { queue: [], active_id: null, awaiting_next_job: false, status: "idle" };
const cardEls = new Map();                 // job_id → card DOM element
const cardCtx = new Map();                 // job_id → per-card state (svg metadata, manual-fit flag, render timer)
let sharedElapsedTimer = null;             // single interval for the sticky-bar progress

const DROP_ZONE_DEFAULT_TEXT = "Drop SVGs here (or click) to add to the queue";

// Pull a readable message out of a fetch Response. FastAPI errors look like
// {"detail":"..."}; plain text is passed through unchanged.
async function readErr(res) {
  const text = await res.text();
  try {
    const data = JSON.parse(text);
    if (data && typeof data === "object" && data.detail) return String(data.detail);
  } catch {}
  return text;
}

// ───── Upload ────────────────────────────────────────────────────────────

fileInput.addEventListener("change", (e) => {
  for (const f of e.target.files) uploadAndQueue(f);
  fileInput.value = "";
});
dropZone.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropZone.classList.add("drag");
});
dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag"));
dropZone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropZone.classList.remove("drag");
  for (const f of e.dataTransfer.files) uploadAndQueue(f);
});

async function uploadAndQueue(file) {
  const label = dropZone.querySelector("span");
  uploadError.hidden = true;
  uploadError.textContent = "";
  dropZone.classList.add("loading");
  label.textContent = `Processing ${file.name}…`;
  const fd = new FormData();
  fd.append("file", file);
  try {
    const res = await fetch("/upload", { method: "POST", body: fd });
    if (!res.ok) throw new Error(await readErr(res));
    const svg = await res.json();

    // Auto-fill layer selections: select all layers on a fresh upload so
    // re-dropping the same file gives a clean reset, regardless of labels.
    const layer_selections = svg.layers.map((l) => ({ index: l.index, label: l.label }));
    if (layer_selections.length === 0) {
      throw new Error("This SVG doesn't contain any Inkscape layers. Open it in Inkscape, add at least one layer, and export again.");
    }

    // Auto-detect paper
    const detected = detectPaper(svg.width_mm, svg.height_mm);
    const portraitDims = PAPER_SIZES[detected.preset];
    const { w, h } = computePaperDims(detected.preset, detected.orientation,
      svg.width_mm || 210, svg.height_mm || 297);

    const jobReq = {
      svg_id: svg.id,
      filename: svg.filename || file.name,
      layer_selections,
      pause_between_layers: true,
      pause_after_job: true,
      paper_w_mm: w,
      paper_h_mm: h,
      margin_top_mm: 0,
      margin_right_mm: 0,
      margin_bottom_mm: 0,
      margin_left_mm: 0,
      fit_content: false,
      transform_scale: 1.0,
      transform_rotation_deg: 0,
      transform_offset_x_mm: 0,
      transform_offset_y_mm: 0,
      speed_pendown: appSettings.speed_pendown_default,
      speed_penup: appSettings.speed_penup_default,
      accel: appSettings.accel_default,
    };
    const jobRes = await fetch("/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(jobReq),
    });
    if (!jobRes.ok) throw new Error(await readErr(jobRes));
    // The card gets created when the WebSocket state update arrives;
    // createCardForJob will fetch the SVG text via /svg/{id} itself.
  } catch (e) {
    uploadError.textContent = `Upload failed: ${e.message}`;
    uploadError.hidden = false;
  } finally {
    dropZone.classList.remove("loading");
    label.textContent = DROP_ZONE_DEFAULT_TEXT;
  }
}

function detectPaper(w_mm, h_mm) {
  if (!w_mm || !h_mm) return { preset: "A4", orientation: "portrait" };
  const rW = Math.round(w_mm * 10) / 10;
  const rH = Math.round(h_mm * 10) / 10;
  for (const [name, p] of Object.entries(PAPER_SIZES)) {
    if (Math.abs(p.w - rW) < 0.5 && Math.abs(p.h - rH) < 0.5) return { preset: name, orientation: "portrait" };
    if (Math.abs(p.h - rW) < 0.5 && Math.abs(p.w - rH) < 0.5) return { preset: name, orientation: "landscape" };
  }
  return { preset: "Custom", orientation: rW >= rH ? "landscape" : "portrait" };
}

function computePaperDims(preset, orientation, customW, customH) {
  if (preset === "Custom") {
    let w = customW || 210;
    let h = customH || 297;
    if (orientation === "landscape" && h > w) [w, h] = [h, w];
    if (orientation === "portrait" && w > h) [w, h] = [h, w];
    return { w, h };
  }
  const p = PAPER_SIZES[preset];
  return orientation === "landscape" ? { w: p.h, h: p.w } : { w: p.w, h: p.h };
}

// ───── Queue rendering ───────────────────────────────────────────────────

function renderQueue() {
  const ids = new Set(serverState.queue.map((j) => j.id));
  // Remove cards for jobs that no longer exist
  for (const id of Array.from(cardEls.keys())) {
    if (!ids.has(id)) {
      cardEls.get(id).remove();
      cardEls.delete(id);
      cardCtx.delete(id);
    }
  }
  // Append/move cards in order
  for (let i = 0; i < serverState.queue.length; i++) {
    const job = serverState.queue[i];
    let card = cardEls.get(job.id);
    if (!card) {
      card = createCardForJob(job);
      cardEls.set(job.id, card);
    }
    if (card.parentElement !== queueList || Array.from(queueList.children).indexOf(card) !== i) {
      queueList.insertBefore(card, queueList.children[i] || null);
    }
    updateCard(card, job);
  }
  queueEmpty.hidden = serverState.queue.length > 0;
  queueControls.hidden = serverState.queue.length === 0;
}

function createCardForJob(job) {
  const frag = jobCardTemplate.content.cloneNode(true);
  const card = frag.querySelector(".job-card");
  card.dataset.id = job.id;

  // Populate paper-size options & defaults from job data
  const paperSize = card.querySelector(".paper-size");
  paperSize.value = guessPresetFromDims(job.paper_w_mm, job.paper_h_mm).preset;
  const orientation = guessPresetFromDims(job.paper_w_mm, job.paper_h_mm).orientation;
  setSegmentedValue(card.querySelector(".orientation"), orientation);

  card.querySelector(".paper-w").value = job.paper_w_mm;
  card.querySelector(".paper-h").value = job.paper_h_mm;
  card.querySelector(".margin-top").value = job.margin_top_mm || 0;
  card.querySelector(".margin-right").value = job.margin_right_mm || 0;
  card.querySelector(".margin-bottom").value = job.margin_bottom_mm || 0;
  card.querySelector(".margin-left").value = job.margin_left_mm || 0;
  card.querySelector(".fit-content").checked = !!job.fit_content;
  card.querySelector(".transform-scale").value = (job.transform_scale ?? 1).toFixed(2);
  card.querySelector(".transform-rotation").value = job.transform_rotation_deg ?? 0;
  card.querySelector(".transform-offset-x").value = job.transform_offset_x_mm ?? 0;
  card.querySelector(".transform-offset-y").value = job.transform_offset_y_mm ?? 0;
  applyOffsetBoundsToCard(card, job.paper_w_mm, job.paper_h_mm);
  card.querySelector(".speed-pendown").value = job.speed_pendown;
  card.querySelector(".speed-penup").value = job.speed_penup;
  card.querySelector(".accel").value = job.accel;
  card.querySelector(".pause-between-layers").checked = job.pause_between_layers;
  card.querySelector(".pause-after-job").checked = job.pause_after_job;

  // Clicking the card header toggles expansion; action buttons stop propagation.
  card.querySelector(".job-card-head").addEventListener("click", () => toggleCardExpanded(card));
  card.querySelectorAll(".job-actions button").forEach((b) =>
    b.addEventListener("click", (e) => e.stopPropagation())
  );
  card.querySelector(".job-delete").addEventListener("click", () => deleteJob(job.id));
  card.querySelector(".job-move-up").addEventListener("click", () => moveJob(job.id, -1));
  card.querySelector(".job-move-down").addEventListener("click", () => moveJob(job.id, +1));
  card.querySelector(".job-requeue").addEventListener("click", () => requeueJob(job.id));

  // Settings changes
  const paperInputs = [
    card.querySelector(".paper-size"),
    card.querySelector(".paper-w"),
    card.querySelector(".paper-h"),
    card.querySelector(".margin-top"),
    card.querySelector(".margin-right"),
    card.querySelector(".margin-bottom"),
    card.querySelector(".margin-left"),
  ];
  paperInputs.forEach((el) => el.addEventListener("input", () => onPaperChange(card)));
  paperInputs.forEach((el) => el.addEventListener("change", () => onPaperChange(card)));
  card.querySelectorAll(".orientation button").forEach((btn) => {
    btn.addEventListener("click", () => {
      setSegmentedValue(card.querySelector(".orientation"), btn.dataset.val);
      onPaperChange(card);
    });
  });
  card.querySelector(".fit-content").addEventListener("change", () => {
    const ctx = cardCtx.get(job.id);
    if (ctx) ctx.fitLocked = true;
    queueCardUpdate(card);
  });
  card.querySelector(".pause-between-layers").addEventListener("change", () => queueCardUpdate(card));
  card.querySelector(".pause-after-job").addEventListener("change", () => queueCardUpdate(card));
  [card.querySelector(".speed-pendown"),
   card.querySelector(".speed-penup"),
   card.querySelector(".accel")]
    .forEach((el) => el.addEventListener("change", () => queueCardUpdate(card)));

  const transformInputs = [
    card.querySelector(".transform-scale"),
    card.querySelector(".transform-rotation"),
    card.querySelector(".transform-offset-x"),
    card.querySelector(".transform-offset-y"),
  ];
  transformInputs.forEach((el) => {
    el.addEventListener("input", () => {
      const j = serverState.queue.find((x) => x.id === card.dataset.id);
      if (!j) return;
      updatePreviewTransform(card, { ...j, ...readTransformFromCard(card) });
    });
    el.addEventListener("change", () => queueCardUpdate(card));
  });

  pairSlider(card, ".transform-scale", ".transform-scale-slider");
  pairSlider(card, ".transform-rotation", ".transform-rotation-slider");
  pairSlider(card, ".transform-offset-x", ".transform-offset-x-slider");
  pairSlider(card, ".transform-offset-y", ".transform-offset-y-slider");
  pairSlider(card, ".speed-pendown", ".speed-pendown-slider");
  pairSlider(card, ".speed-penup", ".speed-penup-slider");
  pairSlider(card, ".accel", ".accel-slider");

  // Collapsible section headers
  card.querySelectorAll(".card-section-head").forEach((head) => {
    head.addEventListener("click", (e) => {
      if (e.target.closest(".card-section-reset")) return;
      head.parentElement.classList.toggle("collapsed");
    });
  });
  // Reset buttons
  card.querySelectorAll(".card-section-reset").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const kind = btn.dataset.reset;
      if (kind === "margins") resetMargins(card);
      else if (kind === "transform") resetTransform(card);
      else if (kind === "parameters") resetParameters(card);
    });
  });

  const ctx = cardCtx.get(job.id) || { svg: null, fitLocked: false };
  cardCtx.set(job.id, ctx);
  if (!ctx.svg || !ctx.svg.text) {
    fetchSvgMeta(job.svg_id).then((meta) => {
      if (meta) {
        ctx.svg = meta;
        renderPreview(card, job);
        renderLayers(card, job);
      }
    });
  } else {
    renderPreview(card, job);
    renderLayers(card, job);
  }

  // Auto-expand if this is the first card in the queue, or the currently-active job.
  const isFirst = serverState.queue.length > 0 && serverState.queue[0].id === job.id;
  if (isFirst || job.id === serverState.active_id) {
    card.classList.add("expanded");
    card.querySelector(".job-body").hidden = false;
  }

  return card;
}

async function fetchSvgMeta(svg_id) {
  try {
    const res = await fetch(`/svg/${svg_id}`);
    if (!res.ok) return null;
    const text = await res.text();
    const parser = new DOMParser();
    const doc = parser.parseFromString(text, "image/svg+xml");
    const root = doc.documentElement;
    const layers = [];
    let index = 0;
    for (const child of root.children) {
      if (child.tagName.toLowerCase() !== "g") continue;
      const mode = child.getAttribute("inkscape:groupmode");
      if (mode !== "layer") continue;
      const label = child.getAttribute("inkscape:label") || `Layer ${index + 1}`;
      layers.push({ index, label, addressable: !!label && /^\d/.test(label) });
      index++;
    }
    return {
      id: svg_id,
      width: root.getAttribute("width") || "",
      height: root.getAttribute("height") || "",
      width_mm: parseDimToMm(root.getAttribute("width") || ""),
      height_mm: parseDimToMm(root.getAttribute("height") || ""),
      viewBox: root.getAttribute("viewBox") || "",
      layers,
      text,
    };
  } catch (e) {
    return null;
  }
}

function parseDimToMm(s) {
  const m = String(s).trim().match(/^([\d.eE+\-]+)\s*(cm|mm|in|px)?$/i);
  if (!m) return null;
  let v = parseFloat(m[1]);
  const unit = (m[2] || "px").toLowerCase();
  if (unit === "cm") return v * 10;
  if (unit === "in") return v * 25.4;
  if (unit === "mm") return v;
  return v * 25.4 / 96;
}

function formatDim(s) {
  const v = parseDimToMm(s);
  return v != null ? `${v.toFixed(1)} mm` : (s || "—");
}

function guessPresetFromDims(w, h) {
  if (!w || !h) return { preset: "A4", orientation: "portrait" };
  const rW = Math.round(w * 10) / 10;
  const rH = Math.round(h * 10) / 10;
  for (const [name, p] of Object.entries(PAPER_SIZES)) {
    if (Math.abs(p.w - rW) < 0.5 && Math.abs(p.h - rH) < 0.5) return { preset: name, orientation: "portrait" };
    if (Math.abs(p.h - rW) < 0.5 && Math.abs(p.w - rH) < 0.5) return { preset: name, orientation: "landscape" };
  }
  return { preset: "Custom", orientation: rW >= rH ? "landscape" : "portrait" };
}

function setSegmentedValue(seg, val) {
  seg.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.val === val));
}

function getSegmentedValue(seg) {
  return seg.querySelector("button.active")?.dataset.val || "portrait";
}

function toggleCardExpanded(card) {
  const body = card.querySelector(".job-body");
  body.hidden = !body.hidden;
  card.classList.toggle("expanded", !body.hidden);
  if (!body.hidden) {
    const job = serverState.queue.find((j) => j.id === card.dataset.id);
    if (job) {
      // Body width was 0 while hidden — now that it's visible, re-measure.
      requestAnimationFrame(() => updatePreviewTransform(card, job));
    }
  }
}

// ───── Per-card updates ──────────────────────────────────────────────────

function updateCard(card, job) {
  const ctx = cardCtx.get(job.id) || {};

  // Track status transitions so we can auto-collapse a card once the next job
  // becomes active. Only flag the transition *into* a terminal state so a card
  // that's been sitting as "completed" on page load isn't surprise-collapsed.
  const prevStatus = ctx.lastSeenStatus;
  if (prevStatus && prevStatus !== job.status &&
      ["completed", "failed", "cancelled"].includes(job.status)) {
    ctx.finishedPendingCollapse = true;
  }
  ctx.lastSeenStatus = job.status;

  const filename = job.filename || "upload.svg";
  card.querySelector(".job-filename").textContent = filename;

  const paperLabel = formatPaperLabel(job);
  const stageCount = job.stages?.length || 0;
  const subParts = [paperLabel];
  if (job.layer_selections?.length) {
    subParts.push(`${job.layer_selections.length} layer${job.layer_selections.length > 1 ? "s" : ""}`);
  }
  if (job.estimated_total_seconds) subParts.push(`~${formatDuration(Math.round(job.estimated_total_seconds))}`);
  card.querySelector(".job-sub").textContent = subParts.join(" · ");

  const pill = card.querySelector(".job-status-pill");
  pill.textContent = statusLabel(job.status);
  pill.className = `job-status-pill status ${job.status}`;

  // Disable editing when job is active
  const activeBlocks = job.id === serverState.active_id &&
    !["queued", "completed", "failed", "cancelled"].includes(job.status);
  card.classList.toggle("active", job.id === serverState.active_id);
  card.classList.toggle("readonly", activeBlocks);
  card.querySelectorAll(".col-form input, .col-form select, .col-form button")
    .forEach((el) => { el.disabled = activeBlocks; });

  // Auto-expand active card
  if (job.id === serverState.active_id && card.querySelector(".job-body").hidden) {
    toggleCardExpanded(card);
  }

  // Auto-collapse a just-finished card once another job is active.
  if (ctx.finishedPendingCollapse &&
      serverState.active_id && serverState.active_id !== job.id) {
    const body = card.querySelector(".job-body");
    if (!body.hidden) {
      body.hidden = true;
      card.classList.remove("expanded");
    }
    ctx.finishedPendingCollapse = false;
  }

  // Re-queue button visible only when the job has actually been plotted at
  // least once (started_at set) AND is now in a terminal state. This avoids
  // the button flashing visible for freshly-uploaded or just-PATCH-requeued
  // jobs in the brief window before the server broadcast lands.
  const requeueBtn = card.querySelector(".job-requeue");
  if (requeueBtn) {
    const isTerminal = ["completed", "failed", "cancelled"].includes(job.status);
    requeueBtn.hidden = !(isTerminal && job.started_at);
  }

  cardCtx.set(job.id, ctx);

  // Preview + layers + stages + plot-info
  if (ctx.svg) {
    renderPreview(card, job);
    renderLayers(card, job);
  }
  renderStages(card, job);
  renderPlotInfo(card, job);
}

function formatPaperLabel(job) {
  const { preset, orientation } = guessPresetFromDims(job.paper_w_mm, job.paper_h_mm);
  if (preset === "Custom") return `${Math.round(job.paper_w_mm)}×${Math.round(job.paper_h_mm)} mm`;
  return `${preset} ${orientation}`;
}

function renderPreview(card, job) {
  const ctx = cardCtx.get(job.id);
  if (!ctx || !ctx.svg) return;
  const previewEl = card.querySelector(".svg-preview");
  if (!previewEl.dataset.rendered) {
    previewEl.innerHTML = `<div class="paper"><div class="paper-margins" hidden></div><div class="paper-content">${ctx.svg.text}</div><div class="pen-cursor" hidden></div></div>`;
    previewEl.dataset.rendered = "1";
  }
  card.querySelector(".svg-dims").textContent = `${formatDim(ctx.svg.width)} × ${formatDim(ctx.svg.height)}`;
  updatePreviewTransform(card, job);
  syncPreviewLayers(card, job);
}

function dispatchValueChange(el) {
  if (!el) return;
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
}

function resetMargins(card) {
  for (const sel of [".margin-top", ".margin-right", ".margin-bottom", ".margin-left"]) {
    const el = card.querySelector(sel);
    if (el) { el.value = 0; dispatchValueChange(el); }
  }
}

function resetTransform(card) {
  const pairs = [
    [".transform-scale", "1.00"],
    [".transform-rotation", 0],
    [".transform-offset-x", 0],
    [".transform-offset-y", 0],
  ];
  for (const [sel, val] of pairs) {
    const el = card.querySelector(sel);
    if (el) { el.value = val; dispatchValueChange(el); }
  }
}

function resetParameters(card) {
  const pairs = [
    [".speed-pendown", appSettings.speed_pendown_default],
    [".speed-penup", appSettings.speed_penup_default],
    [".accel", appSettings.accel_default],
  ];
  for (const [sel, val] of pairs) {
    const el = card.querySelector(sel);
    if (el) { el.value = val; dispatchValueChange(el); }
  }
}

function updateSliderProgress(slider) {
  if (!slider) return;
  const min = parseFloat(slider.min);
  const max = parseFloat(slider.max);
  const val = parseFloat(slider.value);
  if (!isFinite(min) || !isFinite(max) || !isFinite(val) || max <= min) {
    slider.style.setProperty("--progress", "0%");
    return;
  }
  const pct = Math.max(0, Math.min(100, ((val - min) / (max - min)) * 100));
  slider.style.setProperty("--progress", pct + "%");
}

function applyOffsetBoundsToCard(card, paperW, paperH) {
  const ox = card.querySelector(".transform-offset-x");
  const oy = card.querySelector(".transform-offset-y");
  const oxS = card.querySelector(".transform-offset-x-slider");
  const oyS = card.querySelector(".transform-offset-y-slider");
  if (!ox || !oy) return;
  const w = Math.max(1, paperW || 0);
  const h = Math.max(1, paperH || 0);
  ox.min = -w; ox.max = w;
  oy.min = -h; oy.max = h;
  if (oxS) { oxS.min = -w; oxS.max = w; }
  if (oyS) { oyS.min = -h; oyS.max = h; }
  // Clamp any out-of-range values so PATCH stays consistent with the new bounds.
  const cx = parseFloat(ox.value) || 0;
  const cy = parseFloat(oy.value) || 0;
  if (cx < -w || cx > w) ox.value = Math.max(-w, Math.min(w, cx));
  if (cy < -h || cy > h) oy.value = Math.max(-h, Math.min(h, cy));
  if (oxS) { oxS.value = ox.value; updateSliderProgress(oxS); }
  if (oyS) { oyS.value = oy.value; updateSliderProgress(oyS); }
}

function pairSlider(card, numberSel, sliderSel) {
  const number = card.querySelector(numberSel);
  const slider = card.querySelector(sliderSel);
  if (!number || !slider) return;
  slider.value = number.value;
  updateSliderProgress(slider);
  slider.addEventListener("input", () => {
    if (number.value !== slider.value) {
      number.value = slider.value;
      number.dispatchEvent(new Event("input", { bubbles: true }));
    }
    updateSliderProgress(slider);
  });
  slider.addEventListener("change", () => {
    number.dispatchEvent(new Event("change", { bubbles: true }));
  });
  number.addEventListener("input", () => {
    if (slider.value !== number.value) slider.value = number.value;
    updateSliderProgress(slider);
  });
}

function readTransformFromCard(card) {
  return {
    transform_scale: parseFloat(card.querySelector(".transform-scale").value) || 1,
    transform_rotation_deg: parseFloat(card.querySelector(".transform-rotation").value) || 0,
    transform_offset_x_mm: parseFloat(card.querySelector(".transform-offset-x").value) || 0,
    transform_offset_y_mm: parseFloat(card.querySelector(".transform-offset-y").value) || 0,
  };
}

function updatePreviewTransform(card, job) {
  const previewEl = card.querySelector(".svg-preview");
  const paper = previewEl.querySelector(".paper");
  const content = previewEl.querySelector(".paper-content");
  const margins = previewEl.querySelector(".paper-margins");
  if (!paper || !content) return;
  const ctx = cardCtx.get(job.id);
  if (!ctx || !ctx.svg) return;

  const w = job.paper_w_mm, h = job.paper_h_mm;
  if (w <= 0 || h <= 0) return;
  paper.style.aspectRatio = `${w} / ${h}`;

  const svgW = ctx.svg.width_mm || w;
  const svgH = ctx.svg.height_mm || h;
  const mt = job.margin_top_mm, mr = job.margin_right_mm, mb = job.margin_bottom_mm, ml = job.margin_left_mm;
  const aW = Math.max(0, w - ml - mr);
  const aH = Math.max(0, h - mt - mb);
  const fitScale = job.fit_content && aW > 0 && aH > 0 ? Math.min(aW / svgW, aH / svgH) : 1;
  const fW = svgW * fitScale, fH = svgH * fitScale;
  const offX = ml + (aW - fW) / 2;
  const offY = mt + (aH - fH) / 2;

  const userScale = Math.max(0.01, Math.min(5, job.transform_scale ?? 1));
  const rotDeg = job.transform_rotation_deg ?? 0;
  const offX_user = job.transform_offset_x_mm ?? 0;
  const offY_user = job.transform_offset_y_mm ?? 0;

  // Rotated bounding box of the user-scaled content (for extent calc)
  const rad = (rotDeg * Math.PI) / 180;
  const cosA = Math.abs(Math.cos(rad));
  const sinA = Math.abs(Math.sin(rad));
  const sW = fW * userScale, sH = fH * userScale;
  const bboxW = sW * cosA + sH * sinA;
  const bboxH = sW * sinA + sH * cosA;
  const cX = offX + fW / 2 + offX_user;
  const cY = offY + fH / 2 + offY_user;
  const contentLeft = cX - bboxW / 2;
  const contentTop = cY - bboxH / 2;
  const contentRight = cX + bboxW / 2;
  const contentBottom = cY + bboxH / 2;

  const extentW = Math.max(w, contentRight) - Math.min(0, contentLeft);
  const extentH = Math.max(h, contentBottom) - Math.min(0, contentTop);

  const cs = getComputedStyle(previewEl);
  const padX = parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight);
  const padY = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
  const availW = previewEl.clientWidth - padX;
  const availH = previewEl.clientHeight - padY;
  if (availW <= 0 || availH <= 0) return;
  const mmToPx = Math.min(availW / extentW, availH / extentH);
  paper.style.width = `${w * mmToPx}px`;
  paper.style.height = `${h * mmToPx}px`;

  content.style.left = `${(offX / w) * 100}%`;
  content.style.top = `${(offY / h) * 100}%`;
  content.style.width = `${(fW / w) * 100}%`;
  content.style.height = `${(fH / h) * 100}%`;
  content.style.transformOrigin = "center center";
  content.style.transform =
    `translate(${offX_user * mmToPx}px, ${offY_user * mmToPx}px) ` +
    `rotate(${rotDeg}deg) scale(${userScale})`;

  const anyM = mt > 0 || mr > 0 || mb > 0 || ml > 0;
  margins.hidden = !anyM;
  margins.style.top = `${(mt / h) * 100}%`;
  margins.style.left = `${(ml / w) * 100}%`;
  margins.style.right = `${(mr / w) * 100}%`;
  margins.style.bottom = `${(mb / h) * 100}%`;
}

function syncPreviewLayers(card, job) {
  const svgEl = card.querySelector(".paper-content svg");
  if (!svgEl) return;
  const groups = Array.from(svgEl.children).filter(
    (el) => el.tagName.toLowerCase() === "g" && el.getAttribute("inkscape:groupmode") === "layer"
  );
  const selected = new Set(job.layer_selections.map((s) => s.index));
  groups.forEach((g, i) => { g.style.display = selected.has(i) ? "" : "none"; });
}

function renderLayers(card, job) {
  const ctx = cardCtx.get(job.id);
  if (!ctx || !ctx.svg) return;
  const ul = card.querySelector(".layers");
  const selected = new Set(job.layer_selections.map((s) => s.index));
  ul.innerHTML = "";
  for (const layer of ctx.svg.layers) {
    const li = document.createElement("li");
    const checked = selected.has(layer.index);
    li.innerHTML = `
      <label>
        <input type="checkbox" data-index="${layer.index}" ${checked ? "checked" : ""} />
        <span class="layer-label">${escapeHtml(layer.label)}</span>
      </label>`;
    ul.appendChild(li);
  }
  // Attach change handler once
  if (!ul.dataset.wired) {
    ul.addEventListener("change", () => {
      const layers = Array.from(ul.querySelectorAll("input[type=checkbox]:checked")).map((el) =>
        ctx.svg.layers.find((l) => l.index === parseInt(el.dataset.index))
      ).filter(Boolean).map((l) => ({ index: l.index, label: l.label }));
      // Show/hide pause-between-layers only when >1 selected
      card.querySelector(".multi-layer-options").hidden = layers.length < 2;
      // Update the SVG preview visibility
      syncPreviewLayers(card, { ...job, layer_selections: layers });
      queueCardUpdate(card, { layer_selections: layers });
    });
    ul.dataset.wired = "1";
  }
  card.querySelector(".multi-layer-options").hidden = job.layer_selections.length < 2;
}

function renderStages(card, job) {
  const wrap = card.querySelector(".stages-wrap");
  const ol = card.querySelector(".stages");
  if (!job.stages || job.stages.length <= 1) { wrap.hidden = true; ol.innerHTML = ""; return; }
  wrap.hidden = false;
  ol.innerHTML = "";
  job.stages.forEach((s, i) => {
    const li = document.createElement("li");
    li.className = `stage ${s.status}`;
    li.innerHTML = `<span class="stage-num">${i + 1}</span>
      <span class="stage-label">${escapeHtml((s.labels || []).join(", "))}</span>
      <span class="stage-status">${s.status}</span>`;
    ol.appendChild(li);
  });
}

function renderPlotInfo(card, job) {
  const el = card.querySelector(".plot-info");
  if (job.estimated_total_seconds == null) { el.hidden = true; return; }
  el.hidden = false;
  el.querySelector(".est-time").textContent = formatDuration(Math.round(job.estimated_total_seconds));
  el.querySelector(".pendown-dist").textContent = `${(job.distance_pendown_m || 0).toFixed(2)} m`;
  el.querySelector(".total-dist").textContent = `${(job.distance_total_m || 0).toFixed(2)} m`;
  el.querySelector(".pen-lifts").textContent = `${job.pen_lifts || 0}`;
}

function onPaperChange(card) {
  const job = serverState.queue.find((j) => j.id === card.dataset.id);
  if (!job) return;
  const ctx = cardCtx.get(job.id);
  const preset = card.querySelector(".paper-size").value;
  const orientation = getSegmentedValue(card.querySelector(".orientation"));
  const customW = parseFloat(card.querySelector(".paper-w").value) || 210;
  const customH = parseFloat(card.querySelector(".paper-h").value) || 297;
  card.querySelector(".custom-dims").hidden = preset !== "Custom";
  const { w, h } = computePaperDims(preset, orientation, customW, customH);

  const updates = {
    paper_w_mm: w,
    paper_h_mm: h,
    margin_top_mm: parseFloat(card.querySelector(".margin-top").value) || 0,
    margin_right_mm: parseFloat(card.querySelector(".margin-right").value) || 0,
    margin_bottom_mm: parseFloat(card.querySelector(".margin-bottom").value) || 0,
    margin_left_mm: parseFloat(card.querySelector(".margin-left").value) || 0,
  };

  applyOffsetBoundsToCard(card, w, h);
  Object.assign(updates, readTransformFromCard(card));

  // Auto-fit if not user-locked and content exceeds available area
  if (!ctx?.fitLocked && ctx?.svg?.width_mm && ctx?.svg?.height_mm) {
    const aW = updates.paper_w_mm - updates.margin_left_mm - updates.margin_right_mm;
    const aH = updates.paper_h_mm - updates.margin_top_mm - updates.margin_bottom_mm;
    if (ctx.svg.width_mm > aW || ctx.svg.height_mm > aH) {
      card.querySelector(".fit-content").checked = true;
    }
  }
  updates.fit_content = card.querySelector(".fit-content").checked;

  // Update custom inputs to match computed dims (useful if orientation toggled)
  card.querySelector(".paper-w").value = w;
  card.querySelector(".paper-h").value = h;

  queueCardUpdate(card, updates);
}

const cardUpdateTimers = new Map();
function queueCardUpdate(card, immediateUpdates = null) {
  // Coalesce rapid updates into one PATCH per ~250ms per card
  const id = card.dataset.id;
  clearTimeout(cardUpdateTimers.get(id));
  const doUpdate = () => {
    cardUpdateTimers.delete(id);
    sendCardUpdate(card, immediateUpdates);
  };
  cardUpdateTimers.set(id, setTimeout(doUpdate, 150));
}

async function sendCardUpdate(card, immediateUpdates) {
  const job = serverState.queue.find((j) => j.id === card.dataset.id);
  if (!job) return;
  // A PATCH on a non-queued job re-queues it server-side. Hide the requeue
  // button immediately so the user doesn't see a stale "Plot again" ↻ between
  // the PATCH and the broadcast landing.
  const requeueBtn = card.querySelector(".job-requeue");
  if (requeueBtn && job.status !== "queued") requeueBtn.hidden = true;
  const updates = immediateUpdates || {};
  if (!immediateUpdates) {
    const preset = card.querySelector(".paper-size").value;
    const orientation = getSegmentedValue(card.querySelector(".orientation"));
    const { w, h } = computePaperDims(preset, orientation,
      parseFloat(card.querySelector(".paper-w").value) || 210,
      parseFloat(card.querySelector(".paper-h").value) || 297);
    updates.paper_w_mm = w;
    updates.paper_h_mm = h;
    updates.margin_top_mm = parseFloat(card.querySelector(".margin-top").value) || 0;
    updates.margin_right_mm = parseFloat(card.querySelector(".margin-right").value) || 0;
    updates.margin_bottom_mm = parseFloat(card.querySelector(".margin-bottom").value) || 0;
    updates.margin_left_mm = parseFloat(card.querySelector(".margin-left").value) || 0;
    updates.fit_content = card.querySelector(".fit-content").checked;
    Object.assign(updates, readTransformFromCard(card));
    updates.speed_pendown = parseInt(card.querySelector(".speed-pendown").value);
    updates.speed_penup = parseInt(card.querySelector(".speed-penup").value);
    updates.accel = parseInt(card.querySelector(".accel").value);
    updates.pause_between_layers = card.querySelector(".pause-between-layers").checked;
    updates.pause_after_job = card.querySelector(".pause-after-job").checked;
  }
  try {
    await fetch(`/jobs/${card.dataset.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(updates),
    });
  } catch (e) {
    console.error("update failed", e);
  }
  // Refresh visuals locally right away (server will broadcast soon too)
  if (updates.paper_w_mm) updatePreviewTransform(card, { ...job, ...updates });
}

async function deleteJob(id) {
  const res = await fetch(`/jobs/${id}`, { method: "DELETE" });
  if (!res.ok) {
    topMessage.textContent = `Cannot delete: ${await readErr(res)}`;
    topMessage.className = "error";
  }
}

async function requeueJob(id) {
  try {
    const res = await fetch(`/jobs/${id}/requeue`, { method: "POST" });
    if (!res.ok) throw new Error(await readErr(res));
  } catch (e) {
    topMessage.textContent = `Re-queue failed: ${e.message}`;
    topMessage.className = "error";
  }
}

async function moveJob(id, delta) {
  const idx = serverState.queue.findIndex((j) => j.id === id);
  if (idx < 0) return;
  const newIndex = Math.max(0, Math.min(serverState.queue.length - 1, idx + delta));
  if (newIndex === idx) return;
  await fetch(`/jobs/${id}/move`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ new_index: newIndex }),
  });
}

// ───── Top-level controls ────────────────────────────────────────────────

plotBtn.addEventListener("click", () => postAction("/queue/start"));
pauseBtn.addEventListener("click", () => postAction("/queue/pause"));
resumeBtn.addEventListener("click", () => postAction("/queue/resume"));
continueBtn.addEventListener("click", () => postAction("/queue/continue"));
cancelBtn.addEventListener("click", () => postAction("/queue/cancel"));

async function postAction(path) {
  try {
    const res = await fetch(path, { method: "POST" });
    if (!res.ok) throw new Error(await readErr(res));
  } catch (e) {
    topMessage.textContent = `Request failed: ${e.message}`;
    topMessage.className = "error";
  }
}

function applyTopControls() {
  const s = serverState;
  const active = s.active_id ? s.queue.find((j) => j.id === s.active_id) : null;
  const status = active ? active.status : "idle";

  plotBtn.hidden = !!active || s.awaiting_next_job || !s.queue.some((j) => j.status === "queued");
  pauseBtn.hidden = !active || status !== "plotting";
  resumeBtn.hidden = !active || status !== "paused";
  continueBtn.hidden = !(s.awaiting_next_job || (active && status === "awaiting_pen_change"));
  cancelBtn.hidden = !active && !s.awaiting_next_job;

  // Top status pill text
  if (s.awaiting_next_job) {
    statusEl.textContent = statusLabel("awaiting_next_job");
    statusEl.className = "status awaiting_next_job";
    topMessage.textContent = "Ready for the next job. Load paper / swap pen, then click Continue.";
    topMessage.className = "muted";
  } else if (!active) {
    statusEl.textContent = statusLabel("idle");
    statusEl.className = "status idle";
    topMessage.textContent = "";
  } else {
    statusEl.textContent = `${statusLabel(status)}${active.filename ? ` · ${active.filename}` : ""}`;
    statusEl.className = `status ${status}`;
    topMessage.textContent = active.error ? `Error: ${active.error}` :
      (status === "awaiting_pen_change" ? "Swap the pen if needed, then click Continue for the next layer." : "");
    topMessage.className = active.error ? "error" : "muted";
  }

  // Sticky progress bar
  if (active && active.status === "plotting" && active.plotting_started_at && active.estimated_total_seconds > 0) {
    queueProgress.hidden = false;
    startSharedElapsed(active.plotting_started_at, active.estimated_total_seconds);
  } else {
    queueProgress.hidden = true;
    stopSharedElapsed();
  }
}

// ───── Elapsed / progress timer ──────────────────────────────────────────

function startSharedElapsed(startedAt, estTotal) {
  stopSharedElapsed();
  const fill = queueProgress.querySelector(".progress-fill");
  const timeEl = queueProgress.querySelector(".progress-time");
  const render = () => {
    const secs = Math.max(0, Math.floor(Date.now() / 1000 - startedAt));
    const pct = estTotal > 0 ? Math.min(100, (secs / estTotal) * 100) : 0;
    fill.style.width = `${pct}%`;
    const remaining = Math.max(0, estTotal - secs);
    timeEl.textContent = `${formatDuration(Math.round(remaining))} remaining`;
  };
  render();
  sharedElapsedTimer = setInterval(render, 1000);
}

function stopSharedElapsed() {
  if (sharedElapsedTimer) { clearInterval(sharedElapsedTimer); sharedElapsedTimer = null; }
}

function formatDuration(secs) {
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
    : `${m}:${String(s).padStart(2, "0")}`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ───── Settings modal ────────────────────────────────────────────────────

const settingsBtn = $("settings-btn");
const settingsModal = $("settings-modal");
const settingsPlotterModel = $("settings-plotter-model");
const settingsSpeedPendown = $("settings-speed-pendown");
const settingsSpeedPenup = $("settings-speed-penup");
const settingsAccel = $("settings-accel");
settingsBtn.addEventListener("click", openSettings);
$("settings-cancel").addEventListener("click", () => { settingsModal.hidden = true; });
settingsModal.addEventListener("click", (e) => { if (e.target === settingsModal) settingsModal.hidden = true; });
$("settings-save").addEventListener("click", saveSettings);

function applyAppSettings(data) {
  appSettings = {
    plotter_model: data.plotter_model ?? appSettings.plotter_model,
    speed_pendown_default: data.speed_pendown_default ?? appSettings.speed_pendown_default,
    speed_penup_default: data.speed_penup_default ?? appSettings.speed_penup_default,
    accel_default: data.accel_default ?? appSettings.accel_default,
  };
}

async function loadAppSettings() {
  try {
    const res = await fetch("/settings");
    if (!res.ok) return;
    applyAppSettings(await res.json());
  } catch (e) {}
}

async function openSettings() {
  try {
    const res = await fetch("/settings");
    const data = await res.json();
    applyAppSettings(data);
    settingsPlotterModel.value = String(data.plotter_model || 2);
    settingsSpeedPendown.value = String(data.speed_pendown_default ?? 25);
    settingsSpeedPenup.value = String(data.speed_penup_default ?? 75);
    settingsAccel.value = String(data.accel_default ?? 75);
    for (const sel of ["#settings-speed-pendown-slider", "#settings-speed-penup-slider", "#settings-accel-slider"]) {
      const s = document.querySelector(sel);
      const n = document.querySelector(sel.replace("-slider", ""));
      if (s && n) { s.value = n.value; updateSliderProgress(s); }
    }
  } catch (e) {}
  settingsModal.hidden = false;
}

async function saveSettings() {
  try {
    const body = {
      plotter_model: parseInt(settingsPlotterModel.value),
      speed_pendown_default: parseInt(settingsSpeedPendown.value),
      speed_penup_default: parseInt(settingsSpeedPenup.value),
      accel_default: parseInt(settingsAccel.value),
    };
    const res = await fetch("/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(await res.text());
    applyAppSettings(await res.json());
    settingsModal.hidden = true;
  } catch (e) {
    $("settings-message").textContent = `Save failed: ${e.message}`;
    $("settings-message").className = "error";
  }
}

// Wire the settings-modal sliders (they're not inside a card, so createCardForJob doesn't touch them)
for (const base of ["settings-speed-pendown", "settings-speed-penup", "settings-accel"]) {
  const number = $(base);
  const slider = $(base + "-slider");
  if (!number || !slider) continue;
  slider.addEventListener("input", () => {
    if (number.value !== slider.value) number.value = slider.value;
    updateSliderProgress(slider);
  });
  number.addEventListener("input", () => {
    if (slider.value !== number.value) slider.value = number.value;
    updateSliderProgress(slider);
  });
  updateSliderProgress(slider);
}

// Wire collapsible sections + reset button inside the Settings modal
function resetSettingsSpeed() {
  const pairs = [
    ["settings-speed-pendown", "settings-speed-pendown-slider", 25],
    ["settings-speed-penup", "settings-speed-penup-slider", 75],
    ["settings-accel", "settings-accel-slider", 75],
  ];
  for (const [numId, sliderId, val] of pairs) {
    const n = $(numId);
    const s = $(sliderId);
    if (n) n.value = val;
    if (s) { s.value = val; updateSliderProgress(s); }
  }
}

// ───── Shutdown modal ────────────────────────────────────────────────────

const shutdownBtn = $("shutdown-btn");
const shutdownModal = $("shutdown-modal");
const shutdownCancel = $("shutdown-cancel");
const shutdownConfirm = $("shutdown-confirm");
const shutdownMessage = $("shutdown-message");

function openShutdownModal() {
  shutdownMessage.textContent = "";
  shutdownMessage.className = "muted";
  shutdownConfirm.disabled = false;
  shutdownCancel.disabled = false;
  shutdownModal.hidden = false;
}
function closeShutdownModal() { shutdownModal.hidden = true; }

shutdownBtn.addEventListener("click", openShutdownModal);
shutdownCancel.addEventListener("click", closeShutdownModal);
shutdownModal.addEventListener("click", (e) => { if (e.target === shutdownModal) closeShutdownModal(); });
shutdownConfirm.addEventListener("click", async () => {
  shutdownConfirm.disabled = true;
  shutdownCancel.disabled = true;
  shutdownMessage.textContent = "Shutting down…";
  shutdownMessage.className = "muted";
  try {
    const res = await fetch("/system/shutdown", { method: "POST" });
    if (!res.ok) throw new Error(await readErr(res));
    shutdownMessage.textContent = "Shutdown command sent. You can close this tab.";
  } catch (e) {
    shutdownMessage.textContent = `Shutdown failed: ${e.message}`;
    shutdownMessage.className = "error";
    shutdownConfirm.disabled = false;
    shutdownCancel.disabled = false;
  }
});

settingsModal.querySelectorAll(".card-section-head").forEach((head) => {
  head.addEventListener("click", (e) => {
    if (e.target.closest(".card-section-reset")) return;
    head.parentElement.classList.toggle("collapsed");
  });
});
settingsModal.querySelectorAll(".card-section-reset").forEach((btn) => {
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (btn.dataset.reset === "settings-speed") resetSettingsSpeed();
  });
});

// ───── Pen cursor on active job's preview ────────────────────────────────

function updatePenCursor(msg) {
  const active = serverState.active_id ? cardEls.get(serverState.active_id) : null;
  if (!active) return;
  const cursor = active.querySelector(".pen-cursor");
  const job = serverState.queue.find((j) => j.id === serverState.active_id);
  if (!cursor || !job) return;
  cursor.hidden = false;
  cursor.style.left = `${(msg.x_mm / job.paper_w_mm) * 100}%`;
  cursor.style.top = `${(msg.y_mm / job.paper_h_mm) * 100}%`;
  cursor.classList.toggle("pen-down", !!msg.pen_down);
}

function hideAllPenCursors() {
  document.querySelectorAll(".pen-cursor").forEach((c) => { c.hidden = true; });
}

// ───── WebSocket ─────────────────────────────────────────────────────────

function connectWs() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws/state`);
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === "state") {
      serverState = msg;
      renderQueue();
      applyTopControls();
      if (!serverState.active_id || (serverState.queue.find((j) => j.id === serverState.active_id)?.status !== "plotting")) {
        hideAllPenCursors();
      }
    } else if (msg.type === "position") {
      updatePenCursor(msg);
    }
  };
  ws.onclose = () => setTimeout(connectWs, 2000);
}
connectWs();
loadAppSettings();
loadAppVersion();

async function loadAppVersion() {
  try {
    const res = await fetch("/version");
    if (!res.ok) return;
    const data = await res.json();
    const el = $("app-version");
    if (el && data.version) el.textContent = data.version;
  } catch (e) {}
}

window.addEventListener("resize", () => {
  cardEls.forEach((card, id) => {
    const job = serverState.queue.find((j) => j.id === id);
    if (job) updatePreviewTransform(card, job);
  });
});
