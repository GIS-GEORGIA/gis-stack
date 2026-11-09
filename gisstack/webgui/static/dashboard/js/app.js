// Dashboard bootstrap: settings (language / theme), navigation, the connection dialog and the global refresh control.
import { get, put, ApiError } from "./api.js";
import { setTable, setLang, observe, t } from "./i18n.js";
import { h, $, $$, clear, modal, toast } from "./ui.js";
import { app, featureOn } from "./shared.js";
import * as state from "./state.js";
import * as router from "./router.js";
import overview from "./views/overview.js";
import infra from "./views/infra.js";
import service from "./views/service.js";
import monitoring from "./views/monitoring.js";
import backups from "./views/backups.js";
import deployments from "./views/deployments.js";
import logs from "./views/logs.js";
import settings from "./views/settings.js";
import geoserver from "./views/geoserver.js";
import postgis from "./views/postgis.js";
import mapproxy from "./views/mapproxy.js";
import servers from "./views/servers.js";
import security from "./views/security.js";


const NAV = [
  { route: "overview", ico: "▦", label: "მთავარი" },
  { route: "infra", ico: "☰", label: "ინფრასტრუქტურა" },
  { route: "service/postgis", ico: "▤", label: "PostgreSQL / PostGIS", comp: "postgis" },
  { route: "postgis", ico: "◆", label: "PostGIS-ის მენეჯერი", comp: "postgis", feature: "postgis_manager" },
  { route: "service/geoserver", ico: "◍", label: "GeoServer", comp: "geoserver" },
  { route: "geoserver", ico: "◈", label: "GeoServer-ის მენეჯერი", comp: "geoserver", feature: "geoserver_manager" },
  { route: "service/qgis_server", ico: "◧", label: "QGIS Server", comp: "qgis_server" },
  { route: "mapproxy", ico: "◫", label: "MapProxy-ის მენეჯერი", comp: "mapproxy", feature: "mapproxy_manager" },
  { route: "service/mapproxy", ico: "▥", label: "MapProxy", comp: "mapproxy" },
  { route: "service/webmap", ico: "◎", label: "ვებ რუკები", comp: "webmap" },
  { route: "servers", ico: "⌘", label: "სერვერები", feature: "multi_server" },
  { route: "deployments", ico: "↻", label: "ოპერაციები" },
  { route: "monitoring", ico: "∿", label: "მონიტორინგი", feature: "monitoring" },
  { route: "backups", ico: "⛁", label: "ბექაფები" },
  { route: "logs", ico: "≣", label: "ლოგები" },
  { route: "security", ico: "⛨", label: "უსაფრთხოება" },
  { route: "settings", ico: "⚙", label: "პარამეტრები" },
];

function resolvedLang(v) { return v === "ka" || v === "en" ? v : ((navigator.language || "").toLowerCase().startsWith("ka") ? "ka" : "en"); }
function resolvedTheme(v) { return v === "light" || v === "dark" ? v : (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"); }

export function applySettings() {
  const lang = resolvedLang(app.settings.lang);
  document.documentElement.setAttribute("data-theme", resolvedTheme(app.settings.theme));
  setLang(lang);
  $$(".seg button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.lang === lang)));
  $("#btn-theme").setAttribute("aria-label", t("თემის შეცვლა"));
  router.updateTitle();
}
export async function saveSettings(patch) {
  app.settings = Object.assign({}, app.settings, patch);
  applySettings();
  try { await put("/api/v1/settings", patch); } catch (e) { /* applies for this session anyway */ }
}

function buildNav() {
  const cfg = state.getConfig();
  const list = $("#nav-list");
  clear(list);
  for (const n of NAV) {
    if (n.comp && cfg && cfg.components && !cfg.components[n.comp]) continue;
    const off = n.feature && !featureOn(n.feature);
    list.appendChild(h("li", {}, h("a", { class: "navlink" + (off ? " off" : ""), href: "#/" + n.route, "data-route": n.route },
      h("span", { class: "ico", "aria-hidden": "true" }, n.ico), n.label, off ? h("small", {}, "გამორთულია") : null)));
  }
  const cur = router.parse(location.hash);
  const key = cur.name === "service" && cur.arg ? `service/${cur.arg}` : cur.name;
  $$("#nav-list a").forEach((a) => { if (a.getAttribute("data-route") === key) a.setAttribute("aria-current", "page"); });
}

function updateChip() {
  const text = state.describe();
  $("#conn-text").textContent = text || "კავშირი არ არის არჩეული";
  $("#conn-dot").className = "dot";
}
export async function connectDialog() {
  const c = state.getConn();
  const cfgDefault = state.getConfig() || {};
  const project = h("select", { id: "c-project" }, h("option", { value: "" }, "(ნაგულისხმევი კონფიგურაცია)"), ...app.projects.map((p) => h("option", { value: p, selected: p === c.project }, p)));
  const target = h("select", { id: "c-target" }, h("option", { value: "" }, "(როგორც კონფიგურაციაშია)"), ...["native", "compose", "k8s"].map((x) => h("option", { value: x, selected: x === c.target }, x)));
  const local = h("input", { type: "radio", name: "c-mode", id: "c-local", value: "local", checked: c.mode === "local" });
  const ssh = h("input", { type: "radio", name: "c-mode", id: "c-ssh", value: "ssh", checked: c.mode === "ssh" });
  const host = h("input", { id: "c-host", type: "text", value: c.ssh.host, autocomplete: "off", placeholder: "server.example.ge" });
  const user = h("input", { id: "c-user", type: "text", value: c.ssh.user, autocomplete: "off" });
  const port = h("input", { id: "c-port", type: "number", min: 1, max: 65535, value: c.ssh.port });
  const key = h("input", { id: "c-key", type: "text", value: c.ssh.key, autocomplete: "off", placeholder: "~/.ssh/id_ed25519" });
  const sshBox = h("fieldset", { id: "c-sshbox" }, h("legend", {}, "SSH"),
    h("div", { class: "grid cols-2" }, h("div", { class: "field" }, h("label", { for: "c-host" }, "სერვერი"), host), h("div", { class: "field" }, h("label", { for: "c-user" }, "მომხმარებელი"), user),
      h("div", { class: "field" }, h("label", { for: "c-port" }, "პორტი"), port), h("div", { class: "field" }, h("label", { for: "c-key" }, "გასაღების ფაილი"), key)),
    h("small", { class: "muted" }, "გასაღების გზა ინახება მხოლოდ ამ ტაბში (sessionStorage) და არასდროს იწერება დისკზე."));
  const err = h("p", { class: "error-box", role: "alert", style: { padding: "0" } });
  const sync = () => { sshBox.hidden = !ssh.checked; };
  local.addEventListener("change", sync); ssh.addEventListener("change", sync); sync();
  const body = h("form", { id: "c-form", onsubmit: (e) => e.preventDefault() },
    h("div", { class: "field" }, h("label", { for: "c-project" }, "პროექტი"), project, h("small", {}, `ნაგულისხმევი სამიზნე: ${cfgDefault.deploy_target || "-"}`)),
    h("div", { class: "field" }, h("label", { for: "c-target" }, "სამიზნე"), target),
    h("div", { class: "field", role: "radiogroup", "aria-label": "სად" }, h("label", { class: "check" }, local, "ეს კომპიუტერი"), h("label", { class: "check" }, ssh, "სერვერი SSH-ით")),
    sshBox, err);
  const res = await modal({ title: "კავშირი", body, actions: [{ label: "გაუქმება", value: false }, { label: "შენახვა", value: true, kind: "primary" }] });
  if (!res) return false;
  if (ssh.checked && !host.value.trim()) { toast(t("SSH სერვერის სახელი აუცილებელია"), "bad"); return connectDialog(); }
  await applyConn({ project: project.value, target: target.value, mode: ssh.checked ? "ssh" : "local", ssh: { host: host.value.trim(), user: user.value.trim() || "root", port: Number(port.value) || 22, key: key.value.trim() } });
  return true;
}

async function applyConn(next) {
  try {
    await state.setConn(next);
  } catch (e) {
    toast(e.message, "bad");
    return;
  }
  updateChip();
  buildNav();
  await router.show();
}

function initAutoRefresh() {
  let timer = null;
  const sel = $("#auto-refresh");
  const arm = () => {
    clearInterval(timer);
    const s = Number(sel.value);
    if (s > 0) timer = setInterval(() => { if (!document.hidden) manualRefresh(); }, s * 1000);
    try { sessionStorage.setItem("gs_dash_auto", sel.value); } catch (e) { /* ignore */ }
  };
  try { sel.value = sessionStorage.getItem("gs_dash_auto") || "0"; } catch (e) { /* ignore */ }
  sel.addEventListener("change", arm);
  arm();
}
let busy = false;
export async function manualRefresh() {
  if (busy) return;
  busy = true;
  $("#btn-refresh").disabled = true;
  try { await router.refresh(); $("#updated").textContent = t("განახლდა") + " " + new Date().toLocaleTimeString(); }
  catch (e) { toast(e.message, "bad"); }
  finally { busy = false; $("#btn-refresh").disabled = false; }
}

async function boot() {
  try {
    const [cfg, tbl] = await Promise.all([get("/api/v1/dashboard/config"), get("/api/v1/i18n")]);
    app.features = cfg.features; app.version = cfg.version; app.settings = cfg.settings; app.projects = cfg.projects;
    setTable(tbl.en);
  } catch (e) {
    document.getElementById("main").appendChild(h("div", { class: "error-box", role: "alert" }, h("p", { class: "msg" }, e.message)));
    if (e instanceof ApiError && e.code === "feature_disabled") document.getElementById("main").appendChild(h("p", {}, "ჩართვა: export GISSTACK_FEATURES=dashboard"));
    return;
  }
  $("#ver").textContent = app.version;
  $$(".seg button").forEach((b) => b.addEventListener("click", () => saveSettings({ lang: b.dataset.lang })));
  $("#btn-theme").addEventListener("click", () => saveSettings({ theme: { auto: "light", light: "dark", dark: "auto" }[app.settings.theme] || "auto" }));
  $("#conn-chip").addEventListener("click", connectDialog);
  $("#btn-refresh").addEventListener("click", manualRefresh);
  const toggle = $("#nav-toggle"), nav = $("#nav");
  toggle.addEventListener("click", () => { const open = nav.classList.toggle("open"); toggle.setAttribute("aria-expanded", String(open)); });
  nav.addEventListener("click", (e) => { if (e.target.closest("a")) { nav.classList.remove("open"); toggle.setAttribute("aria-expanded", "false"); } });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (app.settings.theme === "auto") applySettings(); });
  observe();
  applySettings();
  initAutoRefresh();
  for (const [name, view] of Object.entries({ overview, infra, service, monitoring, backups, deployments, logs, settings, geoserver, postgis, mapproxy, servers, security })) router.register(name, view);
  try { await state.loadConfig(); } catch (e) { toast(e.message, "bad"); }
  state.onConn(updateChip);
  updateChip();
  buildNav();
  await router.start($("#main"));
  if (!sessionStorage.getItem("gs_dash_conn")) await connectDialog();      // first visit: ask where to look
}
boot();
