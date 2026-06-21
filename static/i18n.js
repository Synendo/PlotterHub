// Lightweight i18n for the Plotter Hub static UI. No build step, no deps.
//
// Strings live in /static/i18n/<lang>.json as flat "dotted.key": "text" pairs.
// Static markup is translated via data-i18n* attributes (see applyStatic);
// dynamic strings in app.js call the global t()/tn() helpers.
//
// The active language is stored client-side in localStorage. The catalog for
// it is fetched before app.js runs (see the bootstrap in index.html), so t()
// is always ready by the time app.js executes.
(function () {
  const SUPPORTED = ["en", "de", "es", "fr", "it", "ja", "ko", "nl", "pt", "zh-Hans"];
  const FALLBACK = "en";
  const STORAGE_KEY = "ph_lang";

  let catalog = {};          // active-language strings
  let fallbackCatalog = {};  // English, used when a key is missing
  let current = FALLBACK;    // language currently rendered (may be a preview)
  let savedLanguage = FALLBACK; // last persisted choice, used to revert a preview
  let pluralRules = null;
  const catalogCache = {};   // lang → parsed catalog, so previews don't refetch
  const changeListeners = []; // run after a live language swap to re-render dynamic text

  // Resolve the language to load: a saved choice wins; otherwise match the
  // browser's preferred languages against what we ship; else English.
  function pickLanguage() {
    let saved = null;
    try { saved = localStorage.getItem(STORAGE_KEY); } catch {}
    if (saved && SUPPORTED.includes(saved)) return saved;

    const tags = (navigator.languages && navigator.languages.length)
      ? navigator.languages
      : [navigator.language || ""];
    for (const raw of tags) {
      const tag = String(raw);
      if (SUPPORTED.includes(tag)) return tag;              // exact (e.g. zh-Hans)
      const lower = tag.toLowerCase();
      if (lower.startsWith("zh")) return "zh-Hans";          // only Simplified shipped
      const base = lower.split("-")[0];
      const hit = SUPPORTED.find((s) => s.toLowerCase().split("-")[0] === base);
      if (hit) return hit;
    }
    return FALLBACK;
  }

  async function loadCatalog(lang) {
    if (catalogCache[lang]) return catalogCache[lang];
    const res = await fetch(`/static/i18n/${lang}.json`, { cache: "no-cache" });
    if (!res.ok) throw new Error(`catalog ${lang}: ${res.status}`);
    const data = await res.json();
    catalogCache[lang] = data;
    return data;
  }

  // Make `lang` the rendered language: swap the catalog, re-apply static markup,
  // and notify listeners so dynamic (JS-built) text re-renders too. Does NOT
  // persist — that's commitLanguage's job.
  async function loadInto(lang) {
    current = lang;
    document.documentElement.lang = lang;
    catalog = lang === FALLBACK
      ? fallbackCatalog
      : await loadCatalog(lang).catch(() => fallbackCatalog);
    try { pluralRules = new Intl.PluralRules(lang); }
    catch { pluralRules = new Intl.PluralRules(FALLBACK); }
    applyStatic(document);
    for (const fn of changeListeners) { try { fn(lang); } catch {} }
  }

  async function init() {
    savedLanguage = pickLanguage();
    fallbackCatalog = await loadCatalog(FALLBACK).catch(() => ({}));
    await loadInto(savedLanguage);
    return current;
  }

  function raw(key) {
    if (Object.prototype.hasOwnProperty.call(catalog, key)) return catalog[key];
    if (Object.prototype.hasOwnProperty.call(fallbackCatalog, key)) return fallbackCatalog[key];
    return null;
  }

  function interpolate(str, params) {
    if (!params) return str;
    return str.replace(/\{(\w+)\}/g, (m, k) =>
      Object.prototype.hasOwnProperty.call(params, k) ? String(params[k]) : m);
  }

  // Translate a key. Missing keys return the key itself so gaps are visible.
  function t(key, params) {
    const s = raw(key);
    return s == null ? key : interpolate(s, params);
  }

  // Plural-aware translate: looks up `<key>.<category>` (one/other/…) using the
  // active language's CLDR plural rules, falling back to `<key>.other`. The
  // count is exposed to interpolation as {count}.
  function tn(key, count, params) {
    const cat = pluralRules ? pluralRules.select(count) : "other";
    const merged = Object.assign({ count }, params);
    for (const variant of [`${key}.${cat}`, `${key}.other`, key]) {
      const s = raw(variant);
      if (s != null) return interpolate(s, merged);
    }
    return key;
  }

  // Apply translations to a DOM subtree:
  //   data-i18n            → textContent
  //   data-i18n-html       → innerHTML (for strings carrying inline markup)
  //   data-i18n-title      → title attribute
  //   data-i18n-aria-label → aria-label attribute
  //   data-i18n-placeholder→ placeholder attribute
  function applyStatic(root) {
    root.querySelectorAll("[data-i18n]").forEach((el) => {
      const s = raw(el.getAttribute("data-i18n"));
      if (s != null) el.textContent = s;
    });
    root.querySelectorAll("[data-i18n-html]").forEach((el) => {
      const s = raw(el.getAttribute("data-i18n-html"));
      if (s != null) el.innerHTML = s;
    });
    for (const attr of ["title", "aria-label", "placeholder", "label"]) {
      root.querySelectorAll(`[data-i18n-${attr}]`).forEach((el) => {
        const s = raw(el.getAttribute(`data-i18n-${attr}`));
        if (s != null) el.setAttribute(attr, s);
      });
    }
  }

  // Live-switch the rendered language without persisting (Settings preview).
  function previewLanguage(lang) {
    if (!SUPPORTED.includes(lang) || lang === current) return Promise.resolve();
    return loadInto(lang);
  }

  // Persist the currently-rendered language (called on Settings → Save).
  function commitLanguage() {
    savedLanguage = current;
    try { localStorage.setItem(STORAGE_KEY, current); } catch {}
  }

  // Restore the last persisted language (called on Settings → Cancel).
  function revertLanguage() {
    if (current === savedLanguage) return Promise.resolve();
    return loadInto(savedLanguage);
  }

  // Register a callback fired after each live language swap. app.js uses this
  // to re-render text it built with t()/tn() at render time.
  function onLanguageChange(fn) { changeListeners.push(fn); }

  window.I18N = { init, t, tn, applyStatic, onLanguageChange,
                  previewLanguage, commitLanguage, revertLanguage,
                  getLanguage: () => current, getSavedLanguage: () => savedLanguage,
                  SUPPORTED };
  window.t = t;
  window.tn = tn;
})();
