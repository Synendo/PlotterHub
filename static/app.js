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
const pausePenUpBtn = $("pause-pen-up-btn");
const resumeBtn = $("resume-btn");
const continueBtn = $("continue-btn");
const calibrateBtn = $("calibrate-btn");
const cancelBtn = $("cancel-btn");
const jobCardTemplate = $("job-card-template");
const queueProgress = $("queue-progress");

function statusLabel(key) {
  return t(`status.${key}`);
}

let appSettings = {
  plotter_model: 2,
  pause_between_layers_default: true,
  pause_after_job_default: true,
  delete_on_complete_default: false,
  disable_motors_on_complete_default: false,
  speed_pendown_default: 25,
  speed_penup_default: 75,
  acceleration_default: 75,
  optimize_svg_default: false,
  optimize_svg_tolerance_default_mm: 0.10,
  optimize_svg_linemerge_default: true,
  optimize_svg_linesimplify_default: true,
  optimize_svg_linesort_default: true,
  optimize_svg_reloop_default: true,
  display_unit: null,
};

// Length-unit conversion. Internal storage and inputs are always mm; this
// table is for display-only formatting. Tolerance (vpype) keeps mm.
const MM_TO = { mm: 1, cm: 0.1, in: 1 / 25.4 };

// Initial-only fallback used when the user hasn't picked a unit yet:
// en-US locale → inches, everywhere else → millimetres. Once the user
// saves a choice, that value wins on every subsequent page load.
function localeDefaultUnit() {
  try {
    const lang = (navigator.language || "").toLowerCase();
    if (lang === "en-us" || lang.startsWith("en-us-")) return "in";
  } catch {}
  return "mm";
}

function effectiveDisplayUnit() {
  const u = appSettings.display_unit;
  if (u === "mm" || u === "cm" || u === "in") return u;
  return localeDefaultUnit();
}

// mm renders as a whole number (paper presets are always integer mm anyway);
// cm and in get one decimal so a 297 mm height shows as "29.7 cm" / "11.7 in".
function formatLengthValue(mm, unit) {
  unit = unit || effectiveDisplayUnit();
  const v = mm * MM_TO[unit];
  return unit === "mm" ? Math.round(v).toString() : v.toFixed(1);
}

function fmtLength(mm, unit) {
  unit = unit || effectiveDisplayUnit();
  if (mm == null || !isFinite(mm)) return "—";
  return `${formatLengthValue(mm, unit)} ${unit}`;
}

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

// Turn a FastAPI error `detail` into a display string. Coded errors arrive as
// {code, params} and are localized via the apierror.* catalog; plain-string
// and structured-message details fall through to their text.
function apiErrText(detail) {
  if (detail && typeof detail === "object") {
    if (detail.code) return t(`apierror.${detail.code}`, detail.params || {});
    if (detail.message) return detail.message;
  }
  return detail != null ? String(detail) : "";
}

// Pull a readable message out of a fetch Response. FastAPI errors look like
// {"detail": ...}; plain text is passed through unchanged.
async function readErr(res) {
  const text = await res.text();
  try {
    const data = JSON.parse(text);
    if (data && typeof data === "object" && data.detail != null) return apiErrText(data.detail);
  } catch {}
  return text;
}

// ───── Upload ────────────────────────────────────────────────────────────

fileInput.addEventListener("change", (e) => {
  handleDroppedFiles(e.target.files);
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
  handleDroppedFiles(e.dataTransfer.files);
});

// The native `accept=".svg,..."` only filters the file picker; drag-and-drop
// bypasses it. Reject non-SVGs up front so the user sees a clean message
// instead of an XML parse error from the server.
function isSvgFile(file) {
  if (file.type === "image/svg+xml") return true;
  return /\.svg$/i.test(file.name || "");
}

function handleDroppedFiles(fileList) {
  const files = Array.from(fileList || []);
  const ok = files.filter(isSvgFile);
  const bad = files.filter((f) => !isSvgFile(f));
  if (bad.length) {
    uploadError.textContent = bad.length === 1
      ? t("upload.not_svg", { name: bad[0].name })
      : t("upload.files_skipped", { count: bad.length });
    uploadError.hidden = false;
  } else {
    uploadError.hidden = true;
    uploadError.textContent = "";
  }
  for (const f of ok) uploadAndQueue(f);
}

async function uploadAndQueue(file) {
  const label = dropZone.querySelector("span");
  uploadError.hidden = true;
  uploadError.textContent = "";
  dropZone.classList.add("loading");
  label.textContent = t("upload.processing", { name: file.name });
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
      throw new Error(t("upload.no_layers"));
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
      pause_between_layers: appSettings.pause_between_layers_default,
      pause_after_job: appSettings.pause_after_job_default,
      delete_on_complete: appSettings.delete_on_complete_default,
      disable_motors_on_complete: appSettings.disable_motors_on_complete_default,
      paper_width_mm: w,
      paper_height_mm: h,
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
      acceleration: appSettings.acceleration_default,
      optimize_svg: appSettings.optimize_svg_default,
      optimize_svg_tolerance_mm: appSettings.optimize_svg_tolerance_default_mm,
      optimize_svg_linemerge: appSettings.optimize_svg_linemerge_default,
      optimize_svg_linesimplify: appSettings.optimize_svg_linesimplify_default,
      optimize_svg_linesort: appSettings.optimize_svg_linesort_default,
      optimize_svg_reloop: appSettings.optimize_svg_reloop_default,
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
    uploadError.textContent = t("upload.failed", { message: e.message });
    uploadError.hidden = false;
  } finally {
    dropZone.classList.remove("loading");
    label.textContent = t("upload.drop_hint");
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

// Inject a "<name> (W × H mm)" option for jobs whose `paper_size_name` does
// not match the auto-detected preset. Returns true if injected. The option
// remembers its dimensions on `dataset.w/h` so swapping orientation works.
function injectNamedCustomOption(selectEl, job) {
  selectEl.querySelectorAll('option[data-named-custom="1"]').forEach((o) => o.remove());
  if (!job.paper_size_name) return false;
  const guessed = guessPresetFromDims(job.paper_width_mm, job.paper_height_mm);
  if (guessed.preset !== "Custom" &&
      guessed.preset.toLowerCase() === job.paper_size_name.toLowerCase()) {
    return false;
  }
  const opt = document.createElement("option");
  opt.value = "__named_custom__";
  opt.dataset.namedCustom = "1";
  opt.dataset.name = job.paper_size_name;
  opt.dataset.w = String(job.paper_width_mm);
  opt.dataset.h = String(job.paper_height_mm);
  opt.textContent = formatPaperOptionLabel(job.paper_size_name, job.paper_width_mm, job.paper_height_mm);
  const customOpt = selectEl.querySelector('option[value="Custom"]');
  selectEl.insertBefore(opt, customOpt);
  return true;
}

function formatPaperOptionLabel(name, w_mm, h_mm) {
  const u = effectiveDisplayUnit();
  return `${name} (${formatLengthValue(w_mm, u)} × ${formatLengthValue(h_mm, u)} ${u})`;
}

// Re-format every option in a paper-size dropdown with the current display
// unit. Static preset options always show portrait dims (matching the
// existing convention); the named-custom option pulls live dims from its
// dataset (kept in sync by readPaperFromCard on orientation flips).
function relabelPaperOptions(selectEl) {
  if (!selectEl) return;
  selectEl.querySelectorAll("option").forEach((opt) => {
    if (opt.value === "Custom") return;
    if (opt.dataset.namedCustom === "1") {
      const w = parseFloat(opt.dataset.w);
      const h = parseFloat(opt.dataset.h);
      opt.textContent = formatPaperOptionLabel(opt.dataset.name, w, h);
      return;
    }
    const dims = PAPER_SIZES[opt.value];
    if (!dims) return;
    opt.textContent = formatPaperOptionLabel(opt.value, dims.w, dims.h);
  });
}

// Resolve the current paper-size selection on a card to {w, h, paper_size_name}.
// Encapsulates the named-custom logic so onPaperChange and sendCardUpdate
// don't both have to know about it.
function readPaperFromCard(card) {
  const sel = card.querySelector(".paper-size");
  const opt = sel.options[sel.selectedIndex];
  const orientation = getSegmentedValue(card.querySelector(".orientation"));
  if (sel.value === "__named_custom__" && opt) {
    let w = parseFloat(opt.dataset.w);
    let h = parseFloat(opt.dataset.h);
    if (orientation === "landscape" && h > w) [w, h] = [h, w];
    if (orientation === "portrait" && w > h) [w, h] = [h, w];
    // Keep the option's stored dims and visible label in sync with orientation
    // toggles so the dropdown text doesn't drift from the actual dimensions.
    opt.dataset.w = String(w);
    opt.dataset.h = String(h);
    opt.textContent = formatPaperOptionLabel(opt.dataset.name, w, h);
    return { w, h, paper_size_name: opt.dataset.name };
  }
  const customW = parseFloat(card.querySelector(".paper-w").value) || 210;
  const customH = parseFloat(card.querySelector(".paper-h").value) || 297;
  const { w, h } = computePaperDims(sel.value, orientation, customW, customH);
  return { w, h, paper_size_name: null };
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
  const ids = new Set(serverState.queue.map((j) => j.job_id));
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
    let card = cardEls.get(job.job_id);
    if (!card) {
      card = createCardForJob(job);
      cardEls.set(job.job_id, card);
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
  card.dataset.id = job.job_id;
  // Cards are cloned from a <template> after I18N.init()'s one-time pass, so
  // translate this card's static labels now.
  I18N.applyStatic(card);

  // Clamp typed-in number values when the user leaves the field. We hook
  // both `focusout` (fires on blur, before change) and `change` in capture
  // phase (covers the Enter-key path that may not blur). After clamping,
  // dispatch input + change so the paired slider re-syncs and queueCardUpdate
  // sees the corrected value. Clamping during typing would mangle
  // partially-entered numbers like "1100" → "110", so we only do it on commit.
  const clampOnLeave = (e) => {
    const el = e.target;
    if (el instanceof HTMLInputElement && el.type === "number") {
      if (clampNumberInput(el)) {
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }
  };
  card.addEventListener("focusout", clampOnLeave);
  card.addEventListener("change", clampOnLeave, true);

  // Populate paper-size options & defaults from job data
  const paperSize = card.querySelector(".paper-size");
  const namedInjected = injectNamedCustomOption(paperSize, job);
  relabelPaperOptions(paperSize);
  if (namedInjected) {
    paperSize.value = "__named_custom__";
  } else {
    paperSize.value = guessPresetFromDims(job.paper_width_mm, job.paper_height_mm).preset;
  }
  const orientation = guessPresetFromDims(job.paper_width_mm, job.paper_height_mm).orientation;
  setSegmentedValue(card.querySelector(".orientation"), orientation);
  // The `custom-dims` block is `hidden` in the template; only `onPaperChange`
  // reveals it. Sync visibility on initial render so a job that arrives with
  // Custom selected actually shows its dimension fields.
  card.querySelector(".custom-dims").hidden = paperSize.value !== "Custom";

  card.querySelector(".paper-w").value = job.paper_width_mm;
  card.querySelector(".paper-h").value = job.paper_height_mm;
  card.querySelector(".margin-top").value = job.margin_top_mm || 0;
  card.querySelector(".margin-right").value = job.margin_right_mm || 0;
  card.querySelector(".margin-bottom").value = job.margin_bottom_mm || 0;
  card.querySelector(".margin-left").value = job.margin_left_mm || 0;
  card.querySelector(".fit-content").checked = !!job.fit_content;
  card.querySelector(".transform-scale").value = (job.transform_scale ?? 1).toFixed(2);
  card.querySelector(".transform-rotation").value = job.transform_rotation_deg ?? 0;
  card.querySelector(".transform-offset-x").value = job.transform_offset_x_mm ?? 0;
  card.querySelector(".transform-offset-y").value = job.transform_offset_y_mm ?? 0;
  applyOffsetBoundsToCard(card, job.paper_width_mm, job.paper_height_mm);
  card.querySelector(".speed-pendown").value = job.speed_pendown;
  card.querySelector(".speed-penup").value = job.speed_penup;
  card.querySelector(".accel").value = job.acceleration;
  card.querySelector(".pause-between-layers").checked = job.pause_between_layers;
  card.querySelector(".pause-after-job").checked = job.pause_after_job;
  card.querySelector(".delete-on-complete").checked = !!job.delete_on_complete;
  card.querySelector(".disable-motors-on-complete").checked = !!job.disable_motors_on_complete;
  card.querySelector(".optimize").checked = !!job.optimize_svg;
  card.querySelector(".optimize-linemerge").checked = job.optimize_svg_linemerge !== false;
  card.querySelector(".optimize-linesimplify").checked = job.optimize_svg_linesimplify !== false;
  card.querySelector(".optimize-linesort").checked = job.optimize_svg_linesort !== false;
  card.querySelector(".optimize-reloop").checked = job.optimize_svg_reloop !== false;
  card.querySelector(".optimize-tolerance").value = (job.optimize_svg_tolerance_mm ?? 0.10).toFixed(2);
  applyOptimizeEnabledStyle(card);

  // Clicking the card header toggles expansion; action buttons stop propagation.
  card.querySelector(".job-card-head").addEventListener("click", () => toggleCardExpanded(card));
  card.querySelectorAll(".job-actions button").forEach((b) =>
    b.addEventListener("click", (e) => e.stopPropagation())
  );
  card.querySelector(".job-delete").addEventListener("click", () => deleteJob(job.job_id));
  card.querySelector(".job-move-up").addEventListener("click", () => moveJob(job.job_id, -1));
  card.querySelector(".job-move-down").addEventListener("click", () => moveJob(job.job_id, +1));
  card.querySelector(".job-requeue").addEventListener("click", () => requeueJob(job.job_id));

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
    const ctx = cardCtx.get(job.job_id);
    if (ctx) ctx.fitLocked = true;
    queueCardUpdate(card);
  });
  card.querySelector(".pause-between-layers").addEventListener("change", () => queueCardUpdate(card));
  card.querySelector(".pause-after-job").addEventListener("change", () => queueCardUpdate(card));
  card.querySelector(".delete-on-complete").addEventListener("change", () => queueCardUpdate(card));
  card.querySelector(".disable-motors-on-complete").addEventListener("change", () => queueCardUpdate(card));
  card.querySelector(".optimize").addEventListener("change", () => {
    // Master ON while every sub-option is off would be a no-op pipeline —
    // re-enable all four so the toggle actually does something.
    const master = card.querySelector(".optimize");
    if (master.checked) {
      const subs = ["linemerge", "linesimplify", "linesort", "reloop"];
      const anyOn = subs.some((s) => card.querySelector(".optimize-" + s).checked);
      if (!anyOn) {
        subs.forEach((s) => { card.querySelector(".optimize-" + s).checked = true; });
      }
    }
    applyOptimizeEnabledStyle(card);
    queueCardUpdate(card);
  });
  ["optimize-linemerge", "optimize-linesimplify", "optimize-linesort", "optimize-reloop"]
    .forEach((cls) => card.querySelector("." + cls).addEventListener("change", () => {
      syncOptimizeMaster(card);
      queueCardUpdate(card);
    }));
  card.querySelector(".optimize-tolerance").addEventListener("change", () => queueCardUpdate(card));
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
      const j = serverState.queue.find((x) => x.job_id === card.dataset.id);
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
      syncSectionCaret(head.parentElement);
    });
    syncSectionCaret(head.parentElement);
  });
  // Reset buttons
  card.querySelectorAll(".card-section-reset").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const kind = btn.dataset.reset;
      if (kind === "margins") resetMargins(card);
      else if (kind === "transform") resetTransform(card);
      else if (kind === "parameters") resetParameters(card);
      else if (kind === "optimize") resetOptimize(card);
    });
  });

  const ctx = cardCtx.get(job.job_id) || { svg: null, fitLocked: false };
  cardCtx.set(job.job_id, ctx);
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
  const isFirst = serverState.queue.length > 0 && serverState.queue[0].job_id === job.job_id;
  if (isFirst || job.job_id === serverState.active_id) {
    card.classList.add("expanded");
    card.querySelector(".job-body").hidden = false;
  }
  syncJobCardCaret(card);

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
      const label = child.getAttribute("inkscape:label") || t("layer.default_label", { n: index + 1 });
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
  return v != null ? fmtLength(v) : (s || "—");
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

function setCaretTooltip(caret, isExpanded) {
  if (caret) caret.title = isExpanded ? t("a11y.collapse") : t("a11y.expand");
}

function syncSectionCaret(section) {
  if (!section) return;
  const caret = section.querySelector(":scope > .card-section-head > .card-section-caret");
  setCaretTooltip(caret, !section.classList.contains("collapsed"));
}

function syncJobCardCaret(card) {
  if (!card) return;
  const caret = card.querySelector(".job-card-head .card-section-caret");
  setCaretTooltip(caret, card.classList.contains("expanded"));
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
  syncJobCardCaret(card);
  if (!body.hidden) {
    const job = serverState.queue.find((j) => j.job_id === card.dataset.id);
    if (job) {
      // Body width was 0 while hidden — now that it's visible, re-measure.
      requestAnimationFrame(() => updatePreviewTransform(card, job));
    }
  }
}

// ───── Per-card updates ──────────────────────────────────────────────────

function updateCard(card, job) {
  const ctx = cardCtx.get(job.job_id) || {};

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
  card.querySelector(".job-filename").textContent = job.name || filename;

  const paperLabel = formatPaperLabel(job);
  const stageCount = job.stages?.length || 0;
  const subParts = [paperLabel];
  const layerCount = (job.layer_selections || []).filter((s) => s.selected !== false).length;
  if (layerCount) {
    subParts.push(tn("job.layers", layerCount));
  }
  if (job.estimated_total_seconds) subParts.push(formatDuration(Math.round(job.estimated_total_seconds)));
  // Surface the SVG-level pre-optimize state on queued cards so the user knows
  // a future "Plot" click won't be instant if their SVG is still in the
  // optimize queue.
  if (job.status === "queued" && job.optimize_svg) {
    const svgInfo = (serverState.svgs || {})[job.svg_id];
    if (svgInfo && svgInfo.status === "optimizing") subParts.push(t("job.optimizing_svg"));
    else if (svgInfo && svgInfo.status === "pending") subParts.push(t("job.waiting_optimize_svg"));
  }
  // And the background-planning state, so the user knows whether the plot
  // click will be instant or still has to compute the estimate.
  if (job.status === "queued" &&
      (job.plan_status === "pending" || job.plan_status === "planning")) {
    subParts.push(job.plan_status === "planning" ? t("job.planning") : t("job.waiting_plan"));
  }
  card.querySelector(".job-sub").textContent = subParts.join(" · ");

  const pill = card.querySelector(".job-status-pill");
  pill.textContent = statusLabel(job.status);
  pill.className = `job-status-pill status ${job.status}`;

  const errorEl = card.querySelector(".job-error");
  if (job.error) {
    errorEl.textContent = job.error;
    errorEl.hidden = false;
  } else {
    errorEl.textContent = "";
    errorEl.hidden = true;
  }

  // Disable editing when job is active
  const activeBlocks = job.job_id === serverState.active_id &&
    !["queued", "completed", "failed", "cancelled"].includes(job.status);
  card.classList.toggle("active", job.job_id === serverState.active_id);
  card.classList.toggle("readonly", activeBlocks);
  card.querySelectorAll(".col-form input, .col-form select, .col-form button")
    .forEach((el) => { el.disabled = activeBlocks; });

  // Auto-expand active card
  if (job.job_id === serverState.active_id && card.querySelector(".job-body").hidden) {
    toggleCardExpanded(card);
  }

  // Auto-collapse a just-finished card once another job is active.
  if (ctx.finishedPendingCollapse &&
      serverState.active_id && serverState.active_id !== job.job_id) {
    const body = card.querySelector(".job-body");
    if (!body.hidden) {
      body.hidden = true;
      card.classList.remove("expanded");
    }
    ctx.finishedPendingCollapse = false;
  }
  syncJobCardCaret(card);

  // Re-queue button visible only when the job has actually been plotted at
  // least once (started_at set) AND is now in a terminal state. This avoids
  // the button flashing visible for freshly-uploaded or just-PATCH-requeued
  // jobs in the brief window before the server broadcast lands.
  const requeueBtn = card.querySelector(".job-requeue");
  if (requeueBtn) {
    const isTerminal = ["completed", "failed", "cancelled"].includes(job.status);
    requeueBtn.hidden = !(isTerminal && job.started_at);
  }

  cardCtx.set(job.job_id, ctx);

  // Preview + layers + stages + plot-info
  if (ctx.svg) {
    renderPreview(card, job);
    renderLayers(card, job);
  }
  renderStages(card, job);
  renderPlotInfo(card, job);
}

function formatPaperLabel(job) {
  const u = effectiveDisplayUnit();
  if (job.paper_size_name) {
    const w = formatLengthValue(job.paper_width_mm, u);
    const h = formatLengthValue(job.paper_height_mm, u);
    return `${job.paper_size_name} (${w} × ${h} ${u})`;
  }
  const { preset, orientation } = guessPresetFromDims(job.paper_width_mm, job.paper_height_mm);
  if (preset === "Custom") {
    const w = formatLengthValue(job.paper_width_mm, u);
    const h = formatLengthValue(job.paper_height_mm, u);
    return `${w}×${h} ${u}`;
  }
  return `${preset} ${orientation}`;
}

function renderPreview(card, job) {
  const ctx = cardCtx.get(job.job_id);
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

function applyOptimizeEnabledStyle(card) {
  const on = card.querySelector(".optimize").checked;
  const opts = card.querySelector("[data-section='optimize'] .optimize-options");
  if (opts) opts.classList.toggle("disabled", !on);
}

// Keep the master toggle consistent with the sub-options: if all four steps
// are off, the master can't do anything, so untick it.
function syncOptimizeMaster(card) {
  const subs = ["optimize-linemerge", "optimize-linesimplify", "optimize-linesort", "optimize-reloop"];
  const anyOn = subs.some((cls) => card.querySelector("." + cls).checked);
  const master = card.querySelector(".optimize");
  if (!anyOn && master.checked) master.checked = false;
  applyOptimizeEnabledStyle(card);
}

function resetOptimize(card) {
  card.querySelector(".optimize").checked = !!appSettings.optimize_svg_default;
  card.querySelector(".optimize-linemerge").checked = !!appSettings.optimize_svg_linemerge_default;
  card.querySelector(".optimize-linesimplify").checked = !!appSettings.optimize_svg_linesimplify_default;
  card.querySelector(".optimize-linesort").checked = !!appSettings.optimize_svg_linesort_default;
  card.querySelector(".optimize-reloop").checked = !!appSettings.optimize_svg_reloop_default;
  card.querySelector(".optimize-tolerance").value =
    (appSettings.optimize_svg_tolerance_default_mm ?? 0.10).toFixed(2);
  applyOptimizeEnabledStyle(card);
  queueCardUpdate(card);
}

function resetParameters(card) {
  const pairs = [
    [".speed-pendown", appSettings.speed_pendown_default],
    [".speed-penup", appSettings.speed_penup_default],
    [".accel", appSettings.acceleration_default],
  ];
  for (const [sel, val] of pairs) {
    const el = card.querySelector(sel);
    if (el) { el.value = val; dispatchValueChange(el); }
  }
}

// Clamp a number input's value to its own min/max attributes. type="number"
// only enforces the bounds via form validation, so a user can still type 200
// into a 1–110 field — this snaps it back on blur/enter. Returns true if the
// value was changed.
function clampNumberInput(el) {
  if (!el || el.type !== "number" || el.value === "") return false;
  const v = parseFloat(el.value);
  if (!isFinite(v)) return false;
  const min = el.min !== "" ? parseFloat(el.min) : -Infinity;
  const max = el.max !== "" ? parseFloat(el.max) : Infinity;
  const c = Math.max(min, Math.min(max, v));
  if (c !== v) {
    el.value = String(c);
    return true;
  }
  return false;
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
  const ctx = cardCtx.get(job.job_id);
  if (!ctx || !ctx.svg) return;

  const w = job.paper_width_mm, h = job.paper_height_mm;
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
  // `selected !== false` keeps backward-compat with old job records that
  // never carried an explicit selected flag.
  const selected = new Set(
    job.layer_selections.filter((s) => s.selected !== false).map((s) => s.index)
  );
  groups.forEach((g, i) => { g.style.display = selected.has(i) ? "" : "none"; });
}

function renderLayers(card, job) {
  const ctx = cardCtx.get(job.job_id);
  if (!ctx || !ctx.svg) return;
  ensureSvgColors(card, ctx);
  const ul = card.querySelector(".layers");
  // Layer entries carry their own metadata (label, type) plus an optional
  // `selected` flag; entries with selected===false stay in the list so the
  // metadata is preserved across toggles.
  const selected = new Set(
    job.layer_selections.filter((s) => s.selected !== false).map((s) => s.index)
  );
  const overrides = new Map(job.layer_selections.map((s) => [s.index, s]));
  ul.innerHTML = "";
  for (const layer of ctx.svg.layers) {
    const li = document.createElement("li");
    const checked = selected.has(layer.index);
    const override = overrides.get(layer.index);
    const displayLabel = (override && override.label) || layer.label;
    const swatch = layerSwatch(
      override && override.type,
      (ctx.svg.layerColors || {})[layer.index] || null,
      ctx.svg.pageColor || null,
    );
    li.innerHTML = `
      <label>
        <input type="checkbox" data-index="${layer.index}" ${checked ? "checked" : ""} />
        ${swatch}
        <span class="layer-label">${escapeHtml(displayLabel)}</span>
      </label>`;
    ul.appendChild(li);
  }
  // Attach change handler once
  if (!ul.dataset.wired) {
    ul.addEventListener("change", () => {
      const cur = serverState.queue.find((j) => j.job_id === card.dataset.id);
      const curOverrides = new Map((cur?.layer_selections || []).map((s) => [s.index, s]));
      const checkedIndices = new Set(
        Array.from(ul.querySelectorAll("input[type=checkbox]:checked"))
          .map((el) => parseInt(el.dataset.index))
      );
      // Walk every SVG layer (not just the checked ones) so deselected
      // layers stay in the list with `selected: false`. Their label/type
      // and per-layer speed overrides survive a toggle off-and-on.
      const layers = ctx.svg.layers.map((l) => {
        const ovr = curOverrides.get(l.index);
        const sel = {
          index: l.index,
          label: (ovr && ovr.label) || l.label,
          selected: checkedIndices.has(l.index),
        };
        if (ovr && ovr.type) sel.type = ovr.type;
        // Carry through API-set per-layer speed overrides — there's no UI
        // control for them, so a checkbox toggle here must not drop them.
        for (const k of ["speed_pendown", "speed_penup", "acceleration"]) {
          if (ovr && ovr[k] != null) sel[k] = ovr[k];
        }
        return sel;
      });
      const selectedCount = layers.filter((l) => l.selected).length;
      card.querySelector(".multi-layer-options").hidden = selectedCount < 2;
      syncPreviewLayers(card, { ...job, layer_selections: layers });
      queueCardUpdate(card, { layer_selections: layers });
    });
    ul.dataset.wired = "1";
  }
  const selectedCount = job.layer_selections.filter((s) => s.selected !== false).length;
  card.querySelector(".multi-layer-options").hidden = selectedCount < 2;
}

// Per-stage status is a fixed small set; translate the known ones and fall
// back to the raw value for anything unexpected.
const STAGE_STATUS_KEYS = { pending: 1, current: 1, done: 1 };
function stageStatusLabel(st) {
  return STAGE_STATUS_KEYS[st] ? t(`stage_status.${st}`) : st;
}

function renderStages(card, job) {
  const wrap = card.querySelector(".stages-wrap");
  const ol = card.querySelector(".stages");
  if (!job.stages || job.stages.length <= 1) { wrap.hidden = true; ol.innerHTML = ""; return; }
  wrap.hidden = false;
  ol.innerHTML = "";
  // Pull each layer's type so stage rows can echo the icon shown in the
  // layer list above. With pause_between_layers=true (the only case where
  // this list is rendered) each stage has exactly one layer, but joining
  // the icons copes if that ever changes.
  const typeByIndex = new Map(
    (job.layer_selections || []).map((s) => [s.index, s.type])
  );
  const ctx = cardCtx.get(job.job_id);
  const layerColors = (ctx && ctx.svg && ctx.svg.layerColors) || {};
  const pageColor = (ctx && ctx.svg && ctx.svg.pageColor) || null;
  job.stages.forEach((s, i) => {
    const li = document.createElement("li");
    li.className = `stage ${s.status}`;
    const icons = (s.layer_indices || [])
      .map((idx) => layerSwatch(typeByIndex.get(idx), layerColors[idx] || null, pageColor))
      .join("");
    li.innerHTML = `<span class="stage-num">${i + 1}</span>
      ${icons}
      <span class="stage-label">${escapeHtml((s.labels || []).join(", "))}</span>
      <span class="stage-status">${escapeHtml(stageStatusLabel(s.status))}</span>`;
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
  const job = serverState.queue.find((j) => j.job_id === card.dataset.id);
  if (!job) return;
  const ctx = cardCtx.get(job.job_id);
  const preset = card.querySelector(".paper-size").value;
  card.querySelector(".custom-dims").hidden = preset !== "Custom";
  const { w, h, paper_size_name } = readPaperFromCard(card);

  const updates = {
    paper_width_mm: w,
    paper_height_mm: h,
    paper_size_name: paper_size_name,
    margin_top_mm: parseFloat(card.querySelector(".margin-top").value) || 0,
    margin_right_mm: parseFloat(card.querySelector(".margin-right").value) || 0,
    margin_bottom_mm: parseFloat(card.querySelector(".margin-bottom").value) || 0,
    margin_left_mm: parseFloat(card.querySelector(".margin-left").value) || 0,
  };

  applyOffsetBoundsToCard(card, w, h);
  Object.assign(updates, readTransformFromCard(card));

  // Auto-fit if not user-locked and content exceeds available area
  if (!ctx?.fitLocked && ctx?.svg?.width_mm && ctx?.svg?.height_mm) {
    const aW = updates.paper_width_mm - updates.margin_left_mm - updates.margin_right_mm;
    const aH = updates.paper_height_mm - updates.margin_top_mm - updates.margin_bottom_mm;
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
  const job = serverState.queue.find((j) => j.job_id === card.dataset.id);
  if (!job) return;
  // A PATCH on a non-queued job re-queues it server-side. Hide the requeue
  // button immediately so the user doesn't see a stale "Plot again" ↻ between
  // the PATCH and the broadcast landing.
  const requeueBtn = card.querySelector(".job-requeue");
  if (requeueBtn && job.status !== "queued") requeueBtn.hidden = true;
  const updates = immediateUpdates || {};
  if (!immediateUpdates) {
    const { w, h, paper_size_name } = readPaperFromCard(card);
    updates.paper_width_mm = w;
    updates.paper_height_mm = h;
    updates.paper_size_name = paper_size_name;
    updates.margin_top_mm = parseFloat(card.querySelector(".margin-top").value) || 0;
    updates.margin_right_mm = parseFloat(card.querySelector(".margin-right").value) || 0;
    updates.margin_bottom_mm = parseFloat(card.querySelector(".margin-bottom").value) || 0;
    updates.margin_left_mm = parseFloat(card.querySelector(".margin-left").value) || 0;
    updates.fit_content = card.querySelector(".fit-content").checked;
    Object.assign(updates, readTransformFromCard(card));
    updates.speed_pendown = parseInt(card.querySelector(".speed-pendown").value);
    updates.speed_penup = parseInt(card.querySelector(".speed-penup").value);
    updates.acceleration = parseInt(card.querySelector(".accel").value);
    updates.pause_between_layers = card.querySelector(".pause-between-layers").checked;
    updates.pause_after_job = card.querySelector(".pause-after-job").checked;
    updates.delete_on_complete = card.querySelector(".delete-on-complete").checked;
    updates.disable_motors_on_complete = card.querySelector(".disable-motors-on-complete").checked;
    updates.optimize_svg = card.querySelector(".optimize").checked;
    updates.optimize_svg_linemerge = card.querySelector(".optimize-linemerge").checked;
    updates.optimize_svg_linesimplify = card.querySelector(".optimize-linesimplify").checked;
    updates.optimize_svg_linesort = card.querySelector(".optimize-linesort").checked;
    updates.optimize_svg_reloop = card.querySelector(".optimize-reloop").checked;
    const tol = parseFloat(card.querySelector(".optimize-tolerance").value);
    if (isFinite(tol) && tol > 0) updates.optimize_svg_tolerance_mm = tol;
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
  if (updates.paper_width_mm) updatePreviewTransform(card, { ...job, ...updates });
}

async function deleteJob(id) {
  const res = await fetch(`/jobs/${id}`, { method: "DELETE" });
  if (!res.ok) {
    topMessage.textContent = t("error.cannot_delete", { message: await readErr(res) });
    topMessage.className = "error";
  }
}

async function requeueJob(id) {
  try {
    const res = await fetch(`/jobs/${id}/requeue`, { method: "POST" });
    if (!res.ok) throw new Error(await readErr(res));
  } catch (e) {
    topMessage.textContent = t("error.requeue_failed", { message: e.message });
    topMessage.className = "error";
  }
}

async function moveJob(id, delta) {
  const idx = serverState.queue.findIndex((j) => j.job_id === id);
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
pausePenUpBtn.addEventListener("click", () => postAction("/queue/pause-at-pen-up"));
resumeBtn.addEventListener("click", () => postAction("/queue/resume"));
continueBtn.addEventListener("click", () => postAction("/queue/continue"));
calibrateBtn.addEventListener("click", () => postAction("/queue/calibrate"));
cancelBtn.addEventListener("click", () => postAction("/queue/cancel"));

async function postAction(path) {
  try {
    const res = await fetch(path, { method: "POST" });
    if (!res.ok) throw new Error(await readErr(res));
  } catch (e) {
    topMessage.textContent = t("error.request_failed", { message: e.message });
    topMessage.className = "error";
  }
}

function applyTopControls() {
  const s = serverState;
  const active = s.active_id ? s.queue.find((j) => j.job_id === s.active_id) : null;
  const status = active ? active.status : "idle";

  plotBtn.hidden = !!active || s.awaiting_next_job || !s.queue.some((j) => j.status === "queued");
  pauseBtn.hidden = !active || status !== "plotting";
  pausePenUpBtn.hidden = !active || status !== "plotting";
  const penUpPending = !!s.pause_at_pen_up_pending;
  pausePenUpBtn.textContent = penUpPending ? t("controls.pausing_pen_up") : t("controls.pause_pen_up");
  pausePenUpBtn.disabled = penUpPending;
  resumeBtn.hidden = !active || status !== "paused";
  continueBtn.hidden = !(s.awaiting_next_job || (active && status === "awaiting_pen_change"));
  // Calibration button: visible only at a pen-change pause when this job has
  // at least one type='calibration' layer. Label switches singular/plural.
  const calLayers = active && status === "awaiting_pen_change"
    ? (active.layer_selections || []).filter((l) => l.type === "calibration")
    : [];
  calibrateBtn.hidden = calLayers.length === 0;
  calibrateBtn.textContent = calLayers.length > 1
    ? t("controls.calibrate_plural")
    : t("controls.calibrate");
  cancelBtn.hidden = !active && !s.awaiting_next_job;

  // Top status pill text
  if (s.awaiting_next_job) {
    statusEl.textContent = statusLabel("awaiting_next_job");
    statusEl.className = "status awaiting_next_job";
    topMessage.textContent = t("msg.awaiting_next_job");
    topMessage.className = "muted";
  } else if (!active) {
    statusEl.textContent = statusLabel("idle");
    statusEl.className = "status idle";
    topMessage.textContent = "";
  } else {
    statusEl.textContent = `${statusLabel(status)}${active.filename ? ` · ${active.filename}` : ""}`;
    statusEl.className = `status ${status}`;
    let msg = "";
    if (active.error) msg = t("msg.error_prefix", { error: active.error });
    else if (status === "awaiting_pen_change") msg = t("msg.awaiting_pen_change");
    else if (status === "awaiting_optimize") msg = t("msg.awaiting_optimize");
    else if (status === "optimizing") msg = t("msg.optimizing");
    topMessage.textContent = msg;
    topMessage.className = active.error ? "error" : "muted";
  }

  // Shutdown button: disabled while the worker is busy so the Pi can't be
  // powered off mid-plot. Safe to shut down only when idle (no active job and
  // not waiting between jobs).
  const busy = !!s.active_id || !!s.awaiting_next_job;
  shutdownBtn.disabled = busy;
  shutdownBtn.title = busy
    ? t("a11y.shutdown_busy")
    : t("a11y.shutdown");

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
    timeEl.textContent = t("progress.remaining", { time: formatDuration(Math.round(remaining)) });
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

// Inline SVG glyphs that approximate the SF Symbols the macOS companion app
// uses (waveform.path / character.text.justify / xmark.triangle.circle.square /
// scope).
const LAYER_TYPE_ICONS = {
  pattern: `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M1 8 Q 3 3 5 8 T 9 8 T 13 8 T 15 8" /></svg>`,
  text: `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><line x1="2" y1="4" x2="14" y2="4"/><line x1="2" y1="8" x2="14" y2="8"/><line x1="2" y1="12" x2="11" y2="12"/></svg>`,
  svg: `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" aria-hidden="true"><circle cx="6" cy="6" r="3.2"/><rect x="7.5" y="7.5" width="6.5" height="6.5"/><polygon points="3,14 9,14 6,9"/></svg>`,
  calibration: `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" aria-hidden="true"><circle cx="8" cy="8" r="4.5"/><line x1="8" y1="1.5" x2="8" y2="14.5"/><line x1="1.5" y1="8" x2="14.5" y2="8"/></svg>`,
  image: `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2" y="3.5" width="12" height="9" rx="1.5"/><circle cx="5.5" cy="6.5" r="1.1"/><path d="M2.5 11.5 L6 8 L8.5 10.5 L10.5 8.5 L13.5 11.5"/></svg>`,
  map: `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 2.5 L2 4 V13.5 L6 12 L10 13.5 L14 12 V2.5 L10 4 Z"/><line x1="6" y1="2.5" x2="6" y2="12"/><line x1="10" y1="4" x2="10" y2="13.5"/></svg>`,
  model: `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 1.6 L14 5 V10 L8 13.4 L2 10 V5 Z"/><path d="M2 5 L8 8.4 L14 5"/><line x1="8" y1="8.4" x2="8" y2="13.4"/><path d="M8 1.6 V6.6 M8 6.6 L2 10 M8 6.6 L14 10"/></svg>`,
};
// Generic glyph for layer types we don't render a dedicated icon for yet
// (e.g. a not-yet-supported "image" layer) — a neutral rounded square.
const LAYER_TYPE_FALLBACK_ICON = `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" aria-hidden="true"><rect x="2.5" y="2.5" width="11" height="11" rx="2.5"/></svg>`;
// CSS color string → "#rrggbb", or null when it can't be parsed (named
// colors, gradients). getComputedStyle yields rgb()/rgba() forms; the SVG's
// own attributes may carry #rgb / #rrggbb / #rrggbbaa.
function colorToHex(c) {
  if (!c) return null;
  c = c.trim().toLowerCase();
  let m = c.match(/^#([0-9a-f]{3})$/);
  if (m) return "#" + m[1].split("").map((x) => x + x).join("");
  if (/^#[0-9a-f]{6}$/.test(c)) return c;
  if (/^#[0-9a-f]{8}$/.test(c)) return c.slice(0, 7);  // drop alpha
  m = c.match(/^rgba?\(([^)]+)\)/);
  if (m) {
    const p = m[1].split(",").map((x) => x.trim());
    if (p.length >= 3) {
      return "#" + p.slice(0, 3).map((n) => {
        const v = Math.max(0, Math.min(255, Math.round(parseFloat(n))));
        return v.toString(16).padStart(2, "0");
      }).join("");
    }
  }
  return null;
}

// True for a color that actually paints something (not none/transparent/α0).
function isPaintedColor(c) {
  if (!c || c === "none" || c === "transparent") return false;
  const m = c.match(/^rgba?\(([^)]+)\)/);
  if (m) {
    const p = m[1].split(",").map((x) => x.trim());
    if (p.length === 4 && parseFloat(p[3]) === 0) return false;
  }
  return true;
}

const SWATCH_DRAW_SELECTOR = "path, line, polyline, polygon, circle, ellipse, rect";

// Representative pen color of a layer <g> — its most common stroke color, or
// null when nothing in it is stroked. A bounded sample keeps a huge SVG cheap.
function resolveLayerColor(layerG) {
  const els = layerG.querySelectorAll(SWATCH_DRAW_SELECTOR);
  const limit = Math.min(els.length, 400);
  const counts = new Map();
  for (let i = 0; i < limit; i++) {
    const stroke = getComputedStyle(els[i]).stroke;
    if (isPaintedColor(stroke)) counts.set(stroke, (counts.get(stroke) || 0) + 1);
  }
  let best = null, bestN = 0;
  for (const [c, n] of counts) if (n > bestN) { best = c; bestN = n; }
  return best ? colorToHex(best) : null;
}

// Read the page + per-layer pen colors off the live preview SVG. Done once
// per SVG — getComputedStyle resolves CSS classes and inheritance, so the
// preview must already be in the DOM (renderPreview runs before renderLayers).
// The pen color is a layer's stroke color; the page color is the SVG's own
// CSS background. Results are cached on ctx.svg.
function ensureSvgColors(card, ctx) {
  if (!ctx || !ctx.svg || ctx.svg.colorsReady) return;
  const svgRoot = card.querySelector(".paper-content svg");
  if (!svgRoot) return;  // preview not in the DOM yet — retried next render
  const bg = getComputedStyle(svgRoot).backgroundColor;
  ctx.svg.pageColor = isPaintedColor(bg) ? colorToHex(bg) : null;
  // Walk top-level layer groups in the same order as fetchSvgMeta so the
  // running index lines up with ctx.svg.layers[].index.
  const layerColors = {};
  let index = 0;
  for (const g of svgRoot.children) {
    if (g.tagName.toLowerCase() !== "g") continue;
    if (g.getAttribute("inkscape:groupmode") !== "layer") continue;
    const c = resolveLayerColor(g);
    if (c) layerColors[index] = c;
    index++;
  }
  ctx.svg.layerColors = layerColors;
  ctx.svg.colorsReady = true;
}

// A layer color swatch: the type icon — or a plain dot when the layer has no
// type — drawn in the layer's pen color on a page-colored circle, mirroring
// how that pen looks on that paper. `penHex`/`pageHex` are "#rrggbb" or null;
// both fall back gracefully when the SVG carries no usable color.
function layerSwatch(type, penHex, pageHex) {
  const pen = penHex || "#9ca3af";   // neutral gray when no pen color found
  const page = pageHex || "#ffffff";
  const inner = (type && LAYER_TYPE_ICONS[type])
    ? LAYER_TYPE_ICONS[type]
    : type
      ? LAYER_TYPE_FALLBACK_ICON          // known-but-unsupported type
      : `<span class="layer-swatch-dot"></span>`;  // no type at all
  // Pen == page is an invisible plot; a faint halo keeps the icon legible.
  const faint = penHex && pageHex && penHex === pageHex ? " faint" : "";
  // Untranslated types (t() echoes the key back) fall back to the raw name.
  let typeLabel = "";
  if (type) {
    const key = `layer_type.${type}`;
    const tr = t(key);
    typeLabel = tr === key ? type : tr;
  }
  const title = penHex
    ? t("swatch.pen_color", { hex: penHex }) + (typeLabel ? ` · ${typeLabel}` : "")
    : typeLabel;
  return `<span class="layer-swatch${faint}" style="background:${page};color:${pen};"`
    + (title ? ` title="${escapeHtml(title)}"` : "")
    + `>${inner}</span>`;
}

// ───── Settings modal ────────────────────────────────────────────────────

const settingsBtn = $("settings-btn");
const settingsModal = $("settings-modal");
const settingsPlotterModel = $("settings-plotter-model");
const settingsApiKey = $("settings-api-key");
const settingsApiKeyCopy = $("settings-api-key-copy");
const settingsPauseBetweenLayers = $("settings-pause-between-layers");
const settingsPauseAfterJob = $("settings-pause-after-job");
const settingsDeleteOnComplete = $("settings-delete-on-complete");
const settingsDisableMotorsOnComplete = $("settings-disable-motors-on-complete");
const settingsSpeedPendown = $("settings-speed-pendown");
const settingsSpeedPenup = $("settings-speed-penup");
const settingsAccel = $("settings-accel");
const settingsOptimize = $("settings-optimize");
const settingsOptimizeLinemerge = $("settings-optimize-linemerge");
const settingsOptimizeLinesimplify = $("settings-optimize-linesimplify");
const settingsOptimizeLinesort = $("settings-optimize-linesort");
const settingsOptimizeReloop = $("settings-optimize-reloop");
const settingsOptimizeTolerance = $("settings-optimize-tolerance");
const settingsDisplayUnit = $("settings-display-unit");
const settingsLanguage = $("settings-language");
settingsBtn.addEventListener("click", openSettings);
// Closing without saving reverts a live language preview to the saved language.
function closeSettings(revertLang) {
  settingsModal.hidden = true;
  if (revertLang) I18N.revertLanguage();
}
$("settings-cancel").addEventListener("click", () => closeSettings(true));
settingsModal.addEventListener("click", (e) => { if (e.target === settingsModal) closeSettings(true); });
// Live-preview the language the moment it's picked; Save keeps it, Cancel reverts.
settingsLanguage?.addEventListener("change", () => I18N.previewLanguage(settingsLanguage.value));
{
  const clampOnLeaveSettings = (e) => {
    const el = e.target;
    if (el instanceof HTMLInputElement && el.type === "number") {
      if (clampNumberInput(el)) {
        // Settings sliders are wired to react to "input" — fire it so the
        // slider thumb snaps to the corrected value too.
        el.dispatchEvent(new Event("input", { bubbles: true }));
      }
    }
  };
  settingsModal.addEventListener("focusout", clampOnLeaveSettings);
  settingsModal.addEventListener("change", clampOnLeaveSettings, true);
}
$("settings-save").addEventListener("click", saveSettings);
settingsApiKeyCopy.addEventListener("click", async () => {
  if (!settingsApiKey.value) return;
  try { await navigator.clipboard.writeText(settingsApiKey.value); }
  catch { settingsApiKey.select(); document.execCommand("copy"); }
  settingsApiKeyCopy.textContent = t("common.copied");
  setTimeout(() => { settingsApiKeyCopy.textContent = t("common.copy"); }, 1200);
});

function applyAppSettings(data) {
  const prevUnit = effectiveDisplayUnit();
  appSettings = {
    plotter_model: data.plotter_model ?? appSettings.plotter_model,
    pause_between_layers_default: data.pause_between_layers_default ?? appSettings.pause_between_layers_default,
    pause_after_job_default: data.pause_after_job_default ?? appSettings.pause_after_job_default,
    delete_on_complete_default: data.delete_on_complete_default ?? appSettings.delete_on_complete_default,
    disable_motors_on_complete_default: data.disable_motors_on_complete_default ?? appSettings.disable_motors_on_complete_default,
    speed_pendown_default: data.speed_pendown_default ?? appSettings.speed_pendown_default,
    speed_penup_default: data.speed_penup_default ?? appSettings.speed_penup_default,
    acceleration_default: data.acceleration_default ?? appSettings.acceleration_default,
    optimize_svg_default: data.optimize_svg_default ?? appSettings.optimize_svg_default,
    optimize_svg_tolerance_default_mm: data.optimize_svg_tolerance_default_mm ?? appSettings.optimize_svg_tolerance_default_mm,
    optimize_svg_linemerge_default: data.optimize_svg_linemerge_default ?? appSettings.optimize_svg_linemerge_default,
    optimize_svg_linesimplify_default: data.optimize_svg_linesimplify_default ?? appSettings.optimize_svg_linesimplify_default,
    optimize_svg_linesort_default: data.optimize_svg_linesort_default ?? appSettings.optimize_svg_linesort_default,
    optimize_svg_reloop_default: data.optimize_svg_reloop_default ?? appSettings.optimize_svg_reloop_default,
    display_unit: data.display_unit ?? appSettings.display_unit,
  };
  if (effectiveDisplayUnit() !== prevUnit) refreshUnitDependentDisplays();
}

function refreshUnitDependentDisplays() {
  cardEls.forEach((card, id) => {
    relabelPaperOptions(card.querySelector(".paper-size"));
    const job = serverState.queue.find((j) => j.job_id === id);
    if (job) updateCard(card, job);
  });
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
    settingsApiKey.value = data.api_key || "";
    settingsPauseBetweenLayers.checked = data.pause_between_layers_default ?? true;
    settingsPauseAfterJob.checked = data.pause_after_job_default ?? true;
    settingsDeleteOnComplete.checked = data.delete_on_complete_default ?? false;
    settingsDisableMotorsOnComplete.checked = data.disable_motors_on_complete_default ?? false;
    settingsSpeedPendown.value = String(data.speed_pendown_default ?? 25);
    settingsSpeedPenup.value = String(data.speed_penup_default ?? 75);
    settingsAccel.value = String(data.acceleration_default ?? 75);
    settingsOptimize.checked = !!(data.optimize_svg_default ?? false);
    settingsOptimizeLinemerge.checked = data.optimize_svg_linemerge_default !== false;
    settingsOptimizeLinesimplify.checked = data.optimize_svg_linesimplify_default !== false;
    settingsOptimizeLinesort.checked = data.optimize_svg_linesort_default !== false;
    settingsOptimizeReloop.checked = data.optimize_svg_reloop_default !== false;
    settingsOptimizeTolerance.value = (data.optimize_svg_tolerance_default_mm ?? 0.10).toFixed(2);
    settingsDisplayUnit.value = data.display_unit || effectiveDisplayUnit();
    if (settingsLanguage) settingsLanguage.value = I18N.getLanguage();
    applySettingsOptimizeEnabledStyle();
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
    const tol = parseFloat(settingsOptimizeTolerance.value);
    const body = {
      plotter_model: parseInt(settingsPlotterModel.value),
      pause_between_layers_default: settingsPauseBetweenLayers.checked,
      pause_after_job_default: settingsPauseAfterJob.checked,
      delete_on_complete_default: settingsDeleteOnComplete.checked,
      disable_motors_on_complete_default: settingsDisableMotorsOnComplete.checked,
      speed_pendown_default: parseInt(settingsSpeedPendown.value),
      speed_penup_default: parseInt(settingsSpeedPenup.value),
      acceleration_default: parseInt(settingsAccel.value),
      optimize_svg_default: settingsOptimize.checked,
      optimize_svg_tolerance_default_mm: isFinite(tol) && tol > 0 ? tol : 0.10,
      optimize_svg_linemerge_default: settingsOptimizeLinemerge.checked,
      optimize_svg_linesimplify_default: settingsOptimizeLinesimplify.checked,
      optimize_svg_linesort_default: settingsOptimizeLinesort.checked,
      optimize_svg_reloop_default: settingsOptimizeReloop.checked,
      display_unit: settingsDisplayUnit.value,
    };
    const res = await fetch("/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(await res.text());
    applyAppSettings(await res.json());
    settingsModal.hidden = true;
    // Language was live-previewed on selection; persist the current choice.
    I18N.commitLanguage();
  } catch (e) {
    $("settings-message").textContent = t("settings.save_failed", { message: e.message });
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

function resetSettingsJobOptions() {
  settingsPauseBetweenLayers.checked = true;
  settingsPauseAfterJob.checked = true;
  settingsDeleteOnComplete.checked = false;
  settingsDisableMotorsOnComplete.checked = false;
}

function resetSettingsDisplay() {
  settingsDisplayUnit.value = localeDefaultUnit();
}

function applySettingsOptimizeEnabledStyle() {
  const optsBody = settingsOptimize?.closest(".card-section-body");
  const opts = optsBody?.querySelector(".optimize-options");
  if (opts) opts.classList.toggle("disabled", !settingsOptimize.checked);
}

function syncSettingsOptimizeMaster() {
  const anyOn = settingsOptimizeLinemerge.checked
             || settingsOptimizeLinesimplify.checked
             || settingsOptimizeLinesort.checked
             || settingsOptimizeReloop.checked;
  if (!anyOn && settingsOptimize.checked) settingsOptimize.checked = false;
  applySettingsOptimizeEnabledStyle();
}

function resetSettingsOptimize() {
  settingsOptimize.checked = true;
  settingsOptimizeLinemerge.checked = true;
  settingsOptimizeLinesimplify.checked = true;
  settingsOptimizeLinesort.checked = true;
  settingsOptimizeReloop.checked = true;
  settingsOptimizeTolerance.value = (0.10).toFixed(2);
  applySettingsOptimizeEnabledStyle();
}

settingsOptimize?.addEventListener("change", () => {
  if (settingsOptimize.checked) {
    const anyOn = settingsOptimizeLinemerge.checked
               || settingsOptimizeLinesimplify.checked
               || settingsOptimizeLinesort.checked
               || settingsOptimizeReloop.checked;
    if (!anyOn) {
      settingsOptimizeLinemerge.checked = true;
      settingsOptimizeLinesimplify.checked = true;
      settingsOptimizeLinesort.checked = true;
      settingsOptimizeReloop.checked = true;
    }
  }
  applySettingsOptimizeEnabledStyle();
});
[settingsOptimizeLinemerge, settingsOptimizeLinesimplify,
 settingsOptimizeLinesort, settingsOptimizeReloop]
  .forEach((el) => el?.addEventListener("change", syncSettingsOptimizeMaster));

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
  shutdownMessage.textContent = t("shutdown.sending");
  shutdownMessage.className = "muted";
  try {
    const res = await fetch("/system/shutdown", { method: "POST" });
    if (!res.ok) throw new Error(await readErr(res));
    shutdownMessage.textContent = t("shutdown.sent");
  } catch (e) {
    shutdownMessage.textContent = t("shutdown.failed", { message: e.message });
    shutdownMessage.className = "error";
    shutdownConfirm.disabled = false;
    shutdownCancel.disabled = false;
  }
});

settingsModal.querySelectorAll(".card-section-head").forEach((head) => {
  head.addEventListener("click", (e) => {
    if (e.target.closest(".card-section-reset")) return;
    head.parentElement.classList.toggle("collapsed");
    syncSectionCaret(head.parentElement);
  });
  syncSectionCaret(head.parentElement);
});
settingsModal.querySelectorAll(".card-section-reset").forEach((btn) => {
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (btn.dataset.reset === "settings-speed") resetSettingsSpeed();
    else if (btn.dataset.reset === "settings-job-options") resetSettingsJobOptions();
    else if (btn.dataset.reset === "settings-optimize") resetSettingsOptimize();
    else if (btn.dataset.reset === "settings-display") resetSettingsDisplay();
  });
});

// ───── Pen cursor on active job's preview ────────────────────────────────

const PEN_POSITION_STATUSES = new Set([
  "plotting", "paused", "awaiting_pen_change", "plotting_calibration", "homing",
]);

function updatePenCursor(msg) {
  const active = serverState.active_id ? cardEls.get(serverState.active_id) : null;
  if (!active) return;
  const cursor = active.querySelector(".pen-cursor");
  const job = serverState.queue.find((j) => j.job_id === serverState.active_id);
  if (!cursor || !job) return;
  const w = job.paper_width_mm, h = job.paper_height_mm;
  // AxiDraw's auto_rotate (on by default) rotates portrait documents
  // (height > width) 90° CCW onto the landscape bed, so the reported physical
  // pen position arrives in that rotated frame: phys_x runs along the document
  // height and phys_y runs (inverted) along the document width. Map it back
  // into the upright document frame the preview shows, otherwise the dot
  // tracks the wrong axis and appears to move up/down instead of following.
  let leftPct, topPct;
  if (h > w) {
    leftPct = ((w - msg.y_mm) / w) * 100;
    topPct = (msg.x_mm / h) * 100;
  } else {
    leftPct = (msg.x_mm / w) * 100;
    topPct = (msg.y_mm / h) * 100;
  }
  cursor.hidden = false;
  cursor.style.left = `${leftPct}%`;
  cursor.style.top = `${topPct}%`;
  cursor.classList.toggle("pen-down", !!msg.pen_down);
}

function hideAllPenCursors() {
  document.querySelectorAll(".pen-cursor").forEach((c) => { c.hidden = true; });
}

function applyPenCursor() {
  const job = serverState.active_id
    ? serverState.queue.find((j) => j.job_id === serverState.active_id)
    : null;
  if (!job || !PEN_POSITION_STATUSES.has(job.status)) {
    hideAllPenCursors();
    return;
  }
  const pos = serverState.last_pen_position;
  if (pos) updatePenCursor(pos);
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
      applyPenCursor();
    } else if (msg.type === "position") {
      updatePenCursor(msg);
    }
  };
  ws.onclose = () => setTimeout(connectWs, 2000);
}
connectWs();
loadAppSettings();
loadAppVersion();
loadUpdateStatus();

async function loadAppVersion() {
  try {
    const res = await fetch("/version");
    if (!res.ok) return;
    const data = await res.json();
    const el = $("app-version");
    if (el && data.version) el.textContent = data.version;
  } catch (e) {}
}

// ───── Update notice ─────────────────────────────────────────────────────

let updateStatus = null;
const updateBanner = $("update-banner");

// On a live language swap, applyStatic() handles all data-i18n markup; re-run
// the render paths that build text via t()/tn() so dynamic copy updates too.
I18N.onLanguageChange(() => {
  applyTopControls();
  cardEls.forEach((card, id) => {
    const job = serverState.queue.find((j) => j.job_id === id);
    if (job) updateCard(card, job);
  });
  if (updateStatus) renderUpdateStatus(updateStatus);
});

function renderUpdateStatus(status) {
  updateStatus = status;

  // Header banner: only when there's a newer version the user hasn't skipped.
  const show = status && status.update_available && !status.skipped;
  if (show) {
    $("update-from").textContent = status.current;
    $("update-to").textContent = status.latest;
  }
  updateBanner.hidden = !show;

  // Settings "About & Updates" pill always reflects the latest known state.
  const cur = $("settings-current-version");
  if (cur) cur.textContent = status ? status.current : "";
  const pill = $("settings-update-pill");
  if (pill) {
    if (!status || status.error) {
      pill.textContent = t("update.check_failed");
      pill.className = "update-pill error";
    } else if (status.update_available) {
      pill.textContent = t("update.available_version", { version: status.latest });
      pill.className = "update-pill available";
    } else {
      pill.textContent = t("update.up_to_date");
      pill.className = "update-pill ok";
    }
  }
  // Settings "Update now" is available whenever there's a newer version —
  // including after the banner was skipped (the deferred-update path).
  const sUpd = $("settings-update-now");
  if (sUpd) sUpd.hidden = !(status && status.update_available);
}

async function loadUpdateStatus() {
  try {
    const res = await fetch("/update/status");
    if (!res.ok) return;
    renderUpdateStatus(await res.json());
  } catch (e) {}
}

$("update-skip-btn").addEventListener("click", async () => {
  if (!updateStatus || !updateStatus.latest) return;
  try {
    const res = await fetch("/update/skip", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version: updateStatus.latest }),
    });
    if (res.ok) renderUpdateStatus(await res.json());
  } catch (e) {}
});


$("settings-check-update").addEventListener("click", async () => {
  const btn = $("settings-check-update");
  btn.disabled = true;
  btn.textContent = t("settings.updates.checking");
  try {
    const res = await fetch("/update/check", { method: "POST" });
    if (res.ok) renderUpdateStatus(await res.json());
  } catch (e) {
  } finally {
    btn.disabled = false;
    btn.textContent = t("settings.updates.check_now");
  }
});

$("update-now-btn").addEventListener("click", () => startUpdate(false));
$("settings-update-now").addEventListener("click", () => {
  settingsModal.hidden = true;
  startUpdate(false);
});
$("update-progress-close").addEventListener("click", () => {
  $("update-progress-modal").hidden = true;
});

// Confirm dialog shown when the app folder has local changes that the update
// would overwrite. On confirm we retry the apply with force=true.
let dirtyConfirmCallback = null;
function openDirtyConfirm(files, onConfirm) {
  dirtyConfirmCallback = onConfirm;
  $("update-confirm-files").textContent = files.length ? files.join("\n") : t("update.unknown_files");
  $("update-confirm-modal").hidden = false;
}
$("update-confirm-cancel").addEventListener("click", () => {
  $("update-confirm-modal").hidden = true;
  dirtyConfirmCallback = null;
});
$("update-confirm-overwrite").addEventListener("click", () => {
  $("update-confirm-modal").hidden = true;
  const cb = dirtyConfirmCallback;
  dirtyConfirmCallback = null;
  if (cb) cb();
});

// Kick off an update and follow it to completion. The service restarts
// mid-flight, so we stream the wrapper's log and watch /version: when the app
// comes back reporting the target version, we reload.
async function startUpdate(dryRun, force = false) {
  const target = updateStatus && updateStatus.latest;
  const modal = $("update-progress-modal");
  const title = $("update-progress-title");
  const logEl = $("update-progress-log");
  const statusEl = $("update-progress-status");
  const closeBtn = $("update-progress-close");

  title.textContent = dryRun ? t("update.dryrun_title") : t("update.progress_title");
  logEl.textContent = "";
  statusEl.textContent = t("update.starting");
  statusEl.className = "muted";
  closeBtn.hidden = true;
  modal.hidden = false;

  let res;
  try {
    res = await fetch("/update/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dry_run: !!dryRun, force: !!force }),
    });
  } catch (e) {
    statusEl.textContent = t("update.could_not_start", { message: e.message });
    statusEl.className = "error";
    closeBtn.hidden = false;
    return;
  }
  if (!res.ok) {
    let detail = null;
    try { detail = (await res.json()).detail; } catch {}
    // Dirty working tree → offer to overwrite (unless we already forced).
    if (res.status === 409 && detail && typeof detail === "object"
        && detail.reason === "dirty" && !force) {
      modal.hidden = true;
      openDirtyConfirm(detail.files || [], () => startUpdate(dryRun, true));
      return;
    }
    const msg = apiErrText(detail) || res.statusText;
    statusEl.textContent = t("update.could_not_start", { message: msg });
    statusEl.className = "error";
    closeBtn.hidden = false;
    return;
  }

  const startedAt = Date.now();
  const TIMEOUT_MS = 5 * 60 * 1000;
  let sawDown = false;

  const finish = (msg, isError) => {
    statusEl.textContent = msg;
    statusEl.className = isError ? "error" : "muted";
    closeBtn.hidden = false;
  };

  const tick = async () => {
    // Stream the wrapper log (may fail while the service is restarting).
    try {
      const r = await fetch("/update/log", { cache: "no-store" });
      if (r.ok) {
        const d = await r.json();
        if (d.log) {
          logEl.textContent = d.log;
          logEl.scrollTop = logEl.scrollHeight;
        }
        if (/update DONE \(dry run\)/.test(d.log)) {
          finish(t("update.dryrun_done"), false);
          return;
        }
      } else {
        sawDown = true;
      }
    } catch (e) {
      sawDown = true; // service down during restart
    }

    // For a real update, completion = the app reappears on the new version.
    if (!dryRun) {
      try {
        const r = await fetch("/version", { cache: "no-store" });
        if (r.ok) {
          const d = await r.json();
          if (target && d.version === target) {
            finish(t("update.updated_reloading", { version: target }), false);
            setTimeout(() => location.reload(), 1500);
            return;
          }
          if (sawDown) statusEl.textContent = t("update.service_back");
        } else {
          sawDown = true;
        }
      } catch (e) {
        sawDown = true;
        statusEl.textContent = t("update.service_restarting");
      }
    }

    if (Date.now() - startedAt > TIMEOUT_MS) {
      finish(t("update.timeout"), true);
      return;
    }
    setTimeout(tick, 1500);
  };
  setTimeout(tick, 1200);
}

window.addEventListener("resize", () => {
  cardEls.forEach((card, id) => {
    const job = serverState.queue.find((j) => j.job_id === id);
    if (job) updatePreviewTransform(card, job);
  });
});
