// Georgian is the source language of every string; English comes from /api/i18n (the same table the desktop GUI and the classic page use).
// Whole strings are looked up first, then fragments on Georgian word boundaries - exactly like gisstack.i18n.tr().
const KA = /[Ⴀ-ჿ]/;
const ATTRS = ["placeholder", "title", "aria-label", "alt"];

export function createTranslator(table) {
  const keys = Object.keys(table).sort((a, b) => b.length - a.length).map((k) => k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const re = keys.length ? new RegExp("(?<![Ⴀ-ჿ])(?:" + keys.join("|") + ")(?![Ⴀ-ჿ])", "g") : null;
  const has = (k) => Object.prototype.hasOwnProperty.call(table, k);
  return function tr(s) {
    if (!s || typeof s !== "string" || !KA.test(s)) return s;
    const core = s.trim();
    if (has(core)) return s.replace(core, () => table[core]);
    return re ? s.replace(re, (m) => table[m]) : s;
  };
}

// ---- DOM side: translates what is on the page and keeps doing so (originals are remembered, so switching back restores them)
let lang = "ka", tr = (s) => s, observer = null;
const origText = new WeakMap(), origAttr = new WeakMap();
let setText = new WeakMap();      // what we last wrote into each text node; reset on a language change so everything is re-evaluated

export function currentLang() { return lang; }
export function t(s) { return lang === "en" ? tr(s) : s; }   // for strings built in JS (toasts, dialogs)

function skip(n) {
  const p = n.nodeType === 3 ? n.parentNode : n;
  return !p || (p.closest && p.closest("script,style,textarea,[data-notr]"));
}
function applyText(n) {
  if (skip(n)) return;
  const cur = n.nodeValue;
  if (setText.get(n) === cur) return;
  let orig = origText.get(n);
  if (orig === undefined || (KA.test(cur) && cur !== orig)) {
    if (KA.test(cur)) { orig = cur; origText.set(n, orig); }
  }
  if (orig === undefined) return;
  const out = lang === "en" ? tr(orig) : orig;
  if (out !== cur) n.nodeValue = out;
  setText.set(n, out);
}
function applyAttrs(el, force) {
  if (!el.getAttribute || skip(el)) return;
  let rec = origAttr.get(el);
  for (const a of ATTRS) {
    if (!el.hasAttribute(a)) continue;
    const cur = el.getAttribute(a);
    if (!force && rec && rec[a] && rec[a].set === cur) continue;
    if (KA.test(cur)) { rec = rec || {}; rec[a] = { orig: cur, set: null }; origAttr.set(el, rec); }
    if (!rec || !rec[a]) continue;
    const out = lang === "en" ? tr(rec[a].orig) : rec[a].orig;
    rec[a].set = out;
    if (out !== cur) el.setAttribute(a, out);
  }
}
export function walk(root, force = false) {
  if (!root) return;
  if (root.nodeType === 3) return applyText(root);
  if (root.nodeType !== 1) return;
  applyAttrs(root, force);
  const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
  let n;
  while ((n = w.nextNode())) { if (n.nodeType === 3) applyText(n); else applyAttrs(n, force); }
}
export function setTable(table) { tr = createTranslator(table || {}); }
export function setLang(l) {
  lang = l === "en" ? "en" : "ka";
  document.documentElement.lang = lang;
  setText = new WeakMap();
  walk(document.body, true);
}
export function observe() {
  if (observer) return;
  observer = new MutationObserver((muts) => {
    for (const m of muts) {
      if (m.type === "characterData") applyText(m.target);
      else if (m.type === "attributes") applyAttrs(m.target, false);
      else m.addedNodes.forEach((n) => walk(n));
    }
  });
  observer.observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ATTRS });
}
