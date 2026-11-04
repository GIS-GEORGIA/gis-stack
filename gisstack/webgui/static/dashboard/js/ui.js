// Small DOM helpers and the shared loading / empty / error states.
import { t } from "./i18n.js";

// h("div", {class: "x", onclick: fn, "aria-label": "..."}, "text", [nodes])  - never parses HTML, so data can never become markup
export function h(tag, props, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "style") Object.assign(e.style, v);           // CSSOM is allowed by the CSP; inline style attributes are not
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
    else if (k === "value") e.value = v;
    else if (k === "checked" || k === "disabled" || k === "selected" || k === "open") e[k] = !!v;
    else e.setAttribute(k, v === true ? "" : String(v));
  }
  append(e, children);
  return e;
}
export function append(parent, children) {
  for (const c of (Array.isArray(children) ? children : [children]).flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    parent.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return parent;
}
export function clear(e) { e.textContent = ""; return e; }
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const STATES = {
  running: ["ok", "●", "მუშაობს"], stopped: ["warn", "■", "გაჩერებულია"], starting: ["info", "◌", "ჩაირთვება"], stopping: ["info", "◌", "ითიშება"],
  failed: ["bad", "✖", "შეცდომა"], missing: ["gray", "○", "არ არის დაყენებული"], unknown: ["gray", "?", "უცნობია"],
};
// state is always conveyed by icon AND text, never by colour alone
export function badge(state, label) {
  const [cls, icon, text] = STATES[state] || STATES.unknown;
  return h("span", { class: "badge " + cls }, h("span", { "aria-hidden": "true" }, icon), label || text);
}
export function pill(kind, text) { return h("span", { class: "badge " + kind }, text); }

export function loading(root, lines = 3) {
  clear(root);
  root.setAttribute("aria-busy", "true");
  append(root, [h("p", { class: "sr-only", role: "status" }, "იტვირთება…"), ...Array.from({ length: lines }, () => h("div", { class: "skeleton", style: { height: "3.2rem", marginBottom: ".6rem" } }))]);
}
export function done(root) { root.removeAttribute("aria-busy"); }
export function empty(root, message, action) {
  done(root);
  clear(root);
  append(root, h("div", { class: "empty" }, h("p", {}, message), action || null));
}
export function errorBox(root, err, retry) {
  done(root);
  clear(root);
  append(root, h("div", { class: "error-box", role: "alert" },
    h("p", { class: "msg" }, "✖ ", err && err.message ? err.message : String(err)),
    err && err.code ? h("p", { class: "muted mono" }, err.code) : null,
    retry ? h("button", { type: "button", class: "btn", onclick: retry }, "კიდევ ერთხელ") : null));
}

export function toast(message, kind = "info", ms = 5000) {
  const box = document.getElementById("toasts");
  const node = h("div", { class: "toast " + kind }, message);
  box.appendChild(node);
  setTimeout(() => node.remove(), ms);
}

// Modal built on <dialog>: focus is trapped by the browser, Esc closes. Resolves with the value of the pressed button (or null).
export function modal({ title, body, actions }) {
  const dlg = document.getElementById("dlg");
  return new Promise((resolve) => {
    clear(dlg);
    const bar = h("menu", {}, ...actions.map((a) => h("button", { type: "button", class: "btn " + (a.kind || ""), onclick: () => { dlg.close(); resolve(a.value); } }, a.label)));
    append(dlg, [h("h2", { id: "dlg-title" }, title), h("div", {}, body), bar]);
    dlg.addEventListener("close", () => resolve(null), { once: true });
    dlg.showModal();
    const first = dlg.querySelector("input,select,button.primary,button");
    if (first) first.focus();
  });
}
export async function confirmBox(message, { danger = false, ok = "დიახ", cancel = "გაუქმება", title = "დადასტურება" } = {}) {
  return (await modal({ title, body: h("p", {}, message), actions: [{ label: cancel, value: false }, { label: ok, value: true, kind: danger ? "danger" : "primary" }] })) === true;
}
// For irreversible actions: the user has to type a word, so a stray Enter can never trigger it.
export async function typedConfirm(message, word, { ok = "დადასტურება", title = "დადასტურება" } = {}) {
  const input = h("input", { type: "text", id: "tc", autocomplete: "off", "aria-describedby": "tc-help" });
  const okBtn = { label: ok, value: true, kind: "danger" };
  const body = h("div", {}, typeof message === "string" ? h("p", {}, message) : message, h("div", { class: "field" }, h("label", { for: "tc" }, `ჩაწერე: ${word}`), input, h("small", { id: "tc-help" }, "დადასტურების გარეშე არაფერი შეიცვლება.")));
  const dlg = document.getElementById("dlg");
  const p = modal({ title, body, actions: [{ label: "გაუქმება", value: false }, okBtn] });
  const btn = Array.from(dlg.querySelectorAll("button")).pop();
  btn.disabled = true;
  input.addEventListener("input", () => { btn.disabled = input.value.trim() !== word; });
  return (await p) === true;
}

export function table(headers, rows, { caption, empty: emptyText } = {}) {
  if (!rows.length && emptyText) return h("div", { class: "empty" }, emptyText);
  return h("div", { class: "table-wrap" }, h("table", {},
    caption ? h("caption", {}, caption) : null,
    h("thead", {}, h("tr", {}, ...headers.map((x) => h("th", { scope: "col" }, x)))),
    h("tbody", {}, ...rows.map((r) => h("tr", {}, ...r.map((c, i) => (i === 0 ? h("th", { scope: "row" }, c) : h("td", {}, c))))))));
}
export function kv(pairs) {
  return h("dl", { class: "kv" }, ...pairs.filter(([, v]) => v !== null && v !== undefined && v !== "").flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));
}
export { t };
