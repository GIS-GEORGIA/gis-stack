// Hash router: "#/overview", "#/service/geoserver", "#/logs?c=caddy". A view is {title, mount(root, params) -> {refresh?, destroy?}}.
import { clear, loading, errorBox } from "./ui.js";
import { t } from "./i18n.js";

const routes = new Map();
let current = null, root = null, currentView = null, currentArg = null;

export function register(path, view) { routes.set(path, view); }
export function parse(hash) {
  const raw = (hash || "").replace(/^#\/?/, "");
  const [pathPart, qs] = raw.split("?");
  const segs = pathPart.split("/").filter(Boolean);
  const params = Object.fromEntries(new URLSearchParams(qs || ""));
  const name = segs[0] || "overview";
  return { name, arg: segs[1] || null, params };
}
export function go(path) { location.hash = "#/" + path; }

export async function show() {
  const { name, arg, params } = parse(location.hash);
  const view = routes.get(name) || routes.get("overview");
  if (current && current.destroy) { try { current.destroy(); } catch (e) { /* leaving anyway */ } }
  current = null;
  clear(root);
  loading(root);
  document.querySelectorAll("#nav-list a").forEach((a) => {
    const on = a.getAttribute("data-route") === (arg && name === "service" ? `service/${arg}` : name);
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  try {
    clear(root);
    current = (await view.mount(root, { arg, params })) || {};
    currentView = view; currentArg = arg;
    updateTitle();
    const h1 = root.querySelector("h1");
    if (h1) { h1.setAttribute("tabindex", "-1"); h1.focus({ preventScroll: true }); }   // screen readers announce the new page
  } catch (e) {
    errorBox(root, e, show);
  }
}
export function updateTitle() { if (currentView) document.title = `${t(currentView.title(currentArg))} · GIS Stack`; }
export function refresh() { return current && current.refresh ? current.refresh() : null; }
export function start(container) {
  root = container;
  window.addEventListener("hashchange", show);
  return show();
}
