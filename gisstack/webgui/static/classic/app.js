"use strict";
const H = {"Content-Type": "application/json", "X-GisStack": "1"};
const $ = (s) => document.querySelector(s);
let CAT, DEF, FILES = {};

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function checks(container, items, name, checked, labelKey) {
  $(container).innerHTML = items.map((it) => {
    const id = it.id || it;
    return `<label><input type="checkbox" name="${name}" value="${esc(id)}" ${checked.includes(id) ? "checked" : ""}> ${esc(it[labelKey || "ka"] || id)}</label>`;
  }).join("");
}
const picked = (name) => [...document.querySelectorAll(`input[name="${name}"]:checked`)].map((e) => e.value);
function opts(sel, items, cur, label) {
  $(sel).innerHTML = items.map((it) => { const v = it.id || it; return `<option value="${esc(v)}" ${v === cur ? "selected" : ""}>${esc(label ? label(it) : (it.ka || v))}</option>`; }).join("");
}
function showErr(msg) { const e = $("#err"); e.textContent = msg || ""; e.style.display = msg ? "block" : "none"; if (msg) e.scrollIntoView({block: "center"}); }

function addLayer(l) {
  l = l || {name: "", title: "", title_ka: "", source: "geoserver", wms_layers: "", url: ""};
  const tr = document.createElement("tr");
  tr.innerHTML = `<td><input class="l-name" value="${esc(l.name)}"></td><td><input class="l-title" value="${esc(l.title)}"></td>
    <td><input class="l-ka" value="${esc(l.title_ka || "")}"></td>
    <td><select class="l-src">${["geoserver","qgis","wms","tiles"].map((s) => `<option ${s === l.source ? "selected" : ""}>${s}</option>`).join("")}</select></td>
    <td><input class="l-wms" value="${esc(l.wms_layers || "")}" placeholder="workspace:layer"></td>
    <td><input class="l-url" value="${esc(l.url || "")}"></td><td><button type="button" class="x">×</button></td>`;
  tr.querySelector(".x").onclick = () => tr.remove();
  $("#layers").appendChild(tr);
}

function fill(d) {
  $("#project").value = d.project; $("#domain").value = d.domain; $("#email").value = d.email;
  opts("#tls", CAT.tls_modes, d.tls);
  checks("#components", CAT.components, "comp", Object.keys(d.components).filter((k) => d.components[k]));
  opts("#pg_version", CAT.postgres_versions, d.postgres.version, (v) => "PostgreSQL " + v);
  $("#pg_db").value = d.postgres.db; $("#pg_user").value = d.postgres.user; $("#pg_password").value = "";
  $("#pg_ram").value = d.postgres.ram_gb; $("#pg_maxconn").value = d.postgres.max_connections;
  $("#pg_remote").checked = d.postgres.remote_access; $("#pg_cidr").value = d.postgres.allowed_cidr;
  checks("#pg_ext", CAT.extensions, "ext", d.postgres.extensions);
  document.querySelector('input[name="ext"][value="postgis"]').disabled = true;
  opts("#gs_channel", Object.keys(CAT.geoserver_channels), d.geoserver.channel,
       (k) => `${CAT.geoserver_channels[k].label} — ${CAT.geoserver_channels[k].version}`);
  $("#gs_heap").value = d.geoserver.heap_mb; $("#tc_heap").value = d.tomcat.heap_mb;
  $("#qgis_procs").value = d.qgis.processes; $("#gs_password").value = "";
  checks("#gs_ext", CAT.geoserver_extensions, "gsext", d.geoserver.extensions, "id");
  opts("#mp_cache", CAT.cache_types, d.mapproxy.cache_type, (v) => v);
  opts("#mp_format", CAT.tile_formats, d.mapproxy.format, (v) => v);
  $("#mp_workers").value = d.mapproxy.workers; $("#mp_basemap").value = d.mapproxy.upstream_basemap;
  checks("#mp_grids", CAT.grids, "grid", d.mapproxy.grids);
  $("#layers").innerHTML = ""; d.mapproxy.layers.forEach(addLayer);
  $("#seed_on").checked = d.mapproxy.seed.enabled; $("#seed_from").value = d.mapproxy.seed.levels[0];
  $("#seed_to").value = d.mapproxy.seed.levels[1]; $("#seed_bbox").value = d.mapproxy.seed.bbox.join(", ");
  $("#wm_ka").value = d.webmap.title_ka; $("#wm_en").value = d.webmap.title_en; $("#wm_lang").value = d.webmap.default_lang;
  $("#wm_lat").value = d.webmap.center[0]; $("#wm_lon").value = d.webmap.center[1]; $("#wm_zoom").value = d.webmap.zoom;
  opts("#wm_basemap", CAT.basemaps, d.webmap.basemap); $("#wm_url").value = d.webmap.basemap_url;
  checks("#targets", CAT.targets, "target", d.targets);
  opts("#runtime", CAT.runtimes, d.runtime, (v) => v);
  $("#k8s_ns").value = d.k8s.namespace; $("#k8s_sc").value = d.k8s.storage_class; $("#k8s_svc").value = d.k8s.service_type;
  opts("#deploy_target", CAT.targets, d.deploy_target);
  document.querySelectorAll('input[name="comp"],#gs_channel').forEach((e) => e.addEventListener("change", tomcatNote));
  tomcatNote();
}

function tomcatNote() {
  const ch = CAT.geoserver_channels[$("#gs_channel").value];
  const gs = picked("comp").includes("geoserver");
  $("#tc_note").textContent = gs
    ? `${ch.label}: Tomcat ${ch.tomcat_major}, Java ${ch.java}. ${ch.tomcat_major === "11" ? "GeoServer 3 არ მუშაობს Tomcat 10.1-ზე." : ""}`
    : "";
}

function collect() {
  const comp = {}; CAT.components.forEach((c) => comp[c.id] = picked("comp").includes(c.id));
  const num = (s) => Number($(s).value);
  const layers = [...document.querySelectorAll("#layers tr")].map((tr) => ({
    name: tr.querySelector(".l-name").value.trim(), title: tr.querySelector(".l-title").value.trim() || tr.querySelector(".l-name").value,
    title_ka: tr.querySelector(".l-ka").value.trim(), source: tr.querySelector(".l-src").value,
    wms_layers: tr.querySelector(".l-wms").value.trim(), url: tr.querySelector(".l-url").value.trim(),
  }));
  const targets = picked("target");
  const cfg = {
    project: $("#project").value.trim(), domain: $("#domain").value.trim(), email: $("#email").value.trim(),
    tls: $("#tls").value, targets, deploy_target: $("#deploy_target").value, runtime: $("#runtime").value,
    components: comp,
    postgres: {version: $("#pg_version").value, db: $("#pg_db").value.trim(), user: $("#pg_user").value.trim(),
      password: $("#pg_password").value, extensions: picked("ext"), ram_gb: num("#pg_ram"), max_connections: num("#pg_maxconn"),
      remote_access: $("#pg_remote").checked, allowed_cidr: $("#pg_cidr").value.trim()},
    tomcat: {heap_mb: num("#tc_heap")},
    geoserver: {channel: $("#gs_channel").value, heap_mb: num("#gs_heap"), admin_password: $("#gs_password").value, extensions: picked("gsext")},
    qgis: {processes: num("#qgis_procs")},
    mapproxy: {grids: picked("grid"), cache_type: $("#mp_cache").value, format: $("#mp_format").value, workers: num("#mp_workers"),
      upstream_basemap: $("#mp_basemap").value, layers,
      seed: {enabled: $("#seed_on").checked, levels: [num("#seed_from"), num("#seed_to")],
             bbox: $("#seed_bbox").value.split(",").map((x) => Number(x.trim()))}},
    webmap: {title_ka: $("#wm_ka").value, title_en: $("#wm_en").value, default_lang: $("#wm_lang").value,
      center: [num("#wm_lat"), num("#wm_lon")], zoom: num("#wm_zoom"), basemap: $("#wm_basemap").value, basemap_url: $("#wm_url").value.trim()},
    k8s: {namespace: $("#k8s_ns").value.trim(), storage_class: $("#k8s_sc").value.trim(), service_type: $("#k8s_svc").value},
  };
  return cfg;
}

async function api(path, body) {
  const r = await fetch(path, {method: "POST", headers: H, body: JSON.stringify(body)});
  if (!r.ok) {
    let msg = r.status + " " + r.statusText;
    try { msg = (await r.json()).error || msg; } catch (e) {}
    throw new Error(msg);
  }
  return r;
}

async function preview() {
  showErr("");
  try {
    const j = await (await api("/api/preview", collect())).json();
    FILES = j.files; const names = Object.keys(FILES);
    $("#filesel").innerHTML = names.map((n) => `<option>${esc(n)}</option>`).join("");
    $("#view").textContent = names.length ? FILES[names[0]] : "";
    const w = $("#warn"); w.style.display = j.warnings.length ? "block" : "none"; w.innerHTML = j.warnings.map(esc).join("<br>");
  } catch (e) { showErr(e.message); }
}
$("#filesel").onchange = () => { $("#view").textContent = FILES[$("#filesel").value] || ""; };

async function zip() {
  showErr("");
  try {
    const r = await api("/api/bundle", collect());
    const blob = await r.blob(), a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = `gisstack-${$("#project").value}.zip`; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  } catch (e) { showErr(e.message); }
}

async function deploy() {
  showErr("");
  const mode = $("#mode").value;
  if (mode === "download") return zip();
  const cfg = collect();
  const where = mode === "ssh" ? `${$("#ssh_user").value}@${$("#ssh_host").value}` : "ამ მანქანაზე";
  if (!confirm(`Deploy (${cfg.deploy_target}) → ${where}.\nეს დააინსტალირებს/გაუშვებს სერვისებს. გავაგრძელო?`)) return;
  $("#btn-deploy").disabled = true;
  const log = $("#log"); log.textContent = "";
  try {
    const body = {config: cfg, mode, ssh: {host: $("#ssh_host").value.trim(), port: Number($("#ssh_port").value), user: $("#ssh_user").value.trim(), key: $("#ssh_key").value.trim()}};
    const plan = exPlan();
    const planned = Object.keys(plan).length > 0;
    let r0 = await fetch(planned ? "/api/existing/execute" : "/api/deploy", {method: "POST", headers: H,
      body: JSON.stringify(planned ? Object.assign({plan, install: true, with_data: $("#ex_data").checked,
        confirm_data_loss: Object.values(plan).some((v) => v.purge && (v.action === "remove" || v.action === "reuse"))}, body) : body)});
    let j0 = await r0.json();
    if (r0.status === 409 && j0.preflight) {
      const icon = {ok: "✔", warn: "!", fail: "✘"};
      log.textContent = j0.preflight.checks.map((c) => ` ${icon[c.status]} ${c.message}${c.detail ? "  [" + c.detail + "]" : ""}`).join("\n") + "\n";
      if (!confirm("წინასწარი შემოწმება ვერ გაიარა. გავაგრძელო მაინც? (არ არის რეკომენდებული)")) throw new Error(j0.error);
      r0 = await fetch("/api/deploy", {method: "POST", headers: H, body: JSON.stringify(Object.assign({skip_preflight: true}, body))});
      j0 = await r0.json();
    }
    if (r0.status === 409 && j0.existing) { renderEx(j0.existing); throw new Error(j0.error + " (იხილე სექცია 7ა)"); }
    if (!r0.ok) throw new Error(j0.error || r0.statusText);
    const job = j0.job;
    let next = 0, done = false;
    while (!done) {
      const j = await (await fetch(`/api/jobs/${job}?since=${next}`)).json();
      if (j.lines.length) { log.textContent += j.lines.join("\n") + "\n"; log.scrollTop = log.scrollHeight; }
      next = j.next; done = j.done;
      if (done) log.textContent += j.ok ? "\n✔ დასრულდა\n" : "\n✘ ვერ დასრულდა\n";
      if (!done) await new Promise((r) => setTimeout(r, 1000));
    }
  } catch (e) { showErr(e.message); }
  $("#btn-deploy").disabled = false;
}

async function runJob(url, body) {
  const log = $("#log"); log.textContent = "";
  const {job} = await (await api(url, body)).json();
  let next = 0, done = false;
  while (!done) {
    const j = await (await fetch(`/api/jobs/${job}?since=${next}`)).json();
    if (j.lines.length) { log.textContent += j.lines.join("\n") + "\n"; log.scrollTop = log.scrollHeight; }
    next = j.next; done = j.done;
    if (done) log.textContent += j.ok ? "\n✔ დასრულდა\n" : "\n✘ ვერ დასრულდა\n";
    if (!done) await new Promise((r) => setTimeout(r, 1000));
  }
}

async function cleanup(components) {
  showErr("");
  const mode = $("#mode").value;
  if (mode === "download") return showErr("გასუფთავებისთვის აირჩიე „ამ მანქანაზე“ ან SSH (სექცია 7, „სად“).");
  const cfg = collect();
  const purge = $("#cl_purge").checked, pg = $("#cl_pg").checked;
  if (pg && !purge) return showErr("PostgreSQL-ის წასაშლელად ჩართე „მონაცემების წაშლაც“.");
  const where = mode === "ssh" ? `${$("#ssh_user").value}@${$("#ssh_host").value}` : "ამ მანქანაზე";
  const what = components ? components.join(", ") : "მთელი სტეკი";
  const msg = `გასუფთავება (${cfg.deploy_target}) → ${where}\nრა: ${what}\n` +
    (purge ? "⚠ მონაცემებიც წაიშლება და ვერ აღდგება!" + (pg ? "\n⚠ PostgreSQL სერვერიც წაიშლება!" : "") : "მონაცემები დარჩება.") + "\nგავაგრძელო?";
  if (!confirm(msg)) return;
  const btns = document.querySelectorAll("button"); btns.forEach((b) => b.disabled = true);
  try {
    await runJob("/api/clean", {config: cfg, mode, components, purge, remove_postgres: pg,
      ssh: {host: $("#ssh_host").value.trim(), port: Number($("#ssh_port").value), user: $("#ssh_user").value.trim(), key: $("#ssh_key").value.trim()}});
  } catch (e) { showErr(e.message); }
  btns.forEach((b) => b.disabled = false);
}

function cleanRows() {
  const box = $("#cleanrows"); box.textContent = "";
  CAT.components.forEach((c) => {
    const row = document.createElement("div"); row.className = "clrow";
    const name = document.createElement("span"); name.textContent = c.ka;
    const b = document.createElement("button"); b.type = "button"; b.className = "sec"; b.textContent = "გასუფთავება";
    b.onclick = () => cleanup([c.id]);
    row.append(name, b); box.append(row);
  });
}

function showMsg(msg) { const e = $("#msg"); e.textContent = msg || ""; e.style.display = msg ? "block" : "none"; }
function sshInfo() { return {host: $("#ssh_host").value.trim(), port: Number($("#ssh_port").value), user: $("#ssh_user").value.trim(), key: $("#ssh_key").value.trim()}; }

function compName(id) { const c = CAT.components.find((x) => x.id === id); return c ? c.ka : id; }
function gridBox(id) { return document.querySelector(`input[name="comp"][value="${id}"]`); }

function buildBars() {
  document.querySelectorAll(".compbar[data-comp]").forEach((bar) => {
    const id = bar.dataset.comp;
    bar.textContent = "";
    const title = document.createElement("b"); title.textContent = compName(id);
    const lab = document.createElement("label");
    const cb = document.createElement("input"); cb.type = "checkbox"; cb.className = "bar-on"; cb.dataset.comp = id;
    cb.onchange = () => { const g = gridBox(id); if (g && !g.disabled) { g.checked = cb.checked; g.dispatchEvent(new Event("change")); } };
    lab.append(cb, " ჩართულია");
    const save = document.createElement("button"); save.type = "button"; save.className = "sec"; save.textContent = "შენახვა";
    save.onclick = () => saveComp(id, false);
    const apply = document.createElement("button"); apply.type = "button"; apply.textContent = "შენახვა და გამოყენება";
    apply.onclick = () => saveComp(id, true);
    bar.append(title, lab, save, apply);
  });
  document.querySelectorAll('input[name="comp"]').forEach((g) => g.addEventListener("change", syncBars));
  syncBars();
}
function syncBars() {
  document.querySelectorAll(".bar-on").forEach((cb) => { const g = gridBox(cb.dataset.comp); if (g) cb.checked = g.checked; });
}

async function saveComp(id, andApply) {
  showErr(""); showMsg("");
  const cfg = collect();
  try {
    if (!andApply) {
      const j = await (await api("/api/save", {config: cfg, component: id})).json();
      await loadProjects(cfg.project);
      showMsg(`✔ ${compName(id)}: შენახულია\n${j.saved}`);
      return;
    }
    const mode = $("#mode").value;
    if (mode === "download") return showErr("გამოსაყენებლად აირჩიე „ამ მანქანაზე“ ან SSH (სექცია 7, „სად“).");
    const where = mode === "ssh" ? `${$("#ssh_user").value}@${$("#ssh_host").value}` : "ამ მანქანაზე";
    if (!confirm(`${compName(id)}: შენახვა და გამოყენება (${cfg.deploy_target}) → ${where}.\nმხოლოდ ეს პროგრამა დაინსტალირდება/გადაიტვირთება. გავაგრძელო?`)) return;
    const btns = document.querySelectorAll("button"); btns.forEach((b) => b.disabled = true);
    try { await runJob("/api/apply", {config: cfg, component: id, save: true, mode, ssh: sshInfo()}); }
    finally { btns.forEach((b) => b.disabled = false); }
  } catch (e) { showErr(e.message); }
}

async function loadProjects(select) {
  const j = await (await fetch("/api/projects")).json();
  const sel = $("#proj_sel");
  sel.innerHTML = j.projects.length ? j.projects.map((n) => `<option>${esc(n)}</option>`).join("") : '<option value="">— ჯერ არაა —</option>';
  if (select) sel.value = select;
}
async function loadProject() {
  showErr(""); showMsg("");
  const name = $("#proj_sel").value;
  if (!name) return;
  try {
    const r = await fetch("/api/projects/" + encodeURIComponent(name));
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    fill(j.config); buildBars();
    showMsg(`✔ ჩაიტვირთა: ${name}`);
  } catch (e) { showErr(e.message); }
}
async function saveProject() {
  showErr(""); showMsg("");
  try {
    const cfg = collect();
    const j = await (await api("/api/save", {config: cfg})).json();
    await loadProjects(cfg.project);
    showMsg(`✔ პროექტი შენახულია\n${j.saved}`);
  } catch (e) { showErr(e.message); }
}

let OPS_TIMER = null, OPS_BUSY = false, LOG_JOB = null;
function opsOk() {
  if ($("#mode").value === "download") { showErr("დიაგნოსტიკისთვის აირჩიე „ამ მანქანაზე“ ან SSH (სექცია 7, „სად“)."); return false; }
  return true;
}
function opsBody(extra) { return Object.assign({config: collect(), mode: $("#mode").value, ssh: sshInfo()}, extra || {}); }

async function refreshStatus(quiet) {
  if (OPS_BUSY) return;
  if (!opsOk()) { $("#st-live").checked = false; setLive(); return; }
  OPS_BUSY = true;
  try {
    const r = await fetch("/api/ops/status", {method: "POST", headers: H, body: JSON.stringify(opsBody())});
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    renderStatus(j); showErr("");
  } catch (e) {
    $("#st-time").textContent = "შეცდომა: " + e.message;
    if (!quiet) showErr(e.message);
  }
  OPS_BUSY = false;
}

function renderStatus(j) {
  $("#st-time").textContent = `${j.target} · ${j.domain} · განახლდა ${j.time} (${j.took} წმ)`;
  $("#st-body").innerHTML = j.rows.map((r) => {
    const addr = r.listen.length ? r.listen.map(esc).join("<br>") : "—";
    const chk = r.checks.length ? r.checks.map((c) => `<span class="${c.ok ? "chk-ok" : "chk-bad"}">${c.ok ? "✔" : "✘"} ${esc(c.label)} ${esc(c.code)}</span>`).join("<br>") : "—";
    const links = r.urls.map((u) => `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(u)}</a>`).join("<br>") || "—";
    const auto = r.enabled === null ? "—" : `<input type="checkbox" class="op-auto" data-c="${esc(r.id)}" ${r.enabled ? "checked" : ""}>`;
    const rs = r.restarts ? ` · restarts ${r.restarts}` : "";
    return `<tr><td><b>${esc(r.name)}</b><div class="note">${esc(r.unit)}</div></td>
      <td><span class="badge st-${esc(r.state)}" title="${esc(r.since)}">${esc(r.state)}</span><div class="note" title="${esc(r.since)}">${esc(r.detail)}${rs}</div></td>
      <td>${auto}</td><td>${r.pid || "—"}</td><td class="addr">${addr}</td><td>${chk}</td><td>${links}</td>
      <td class="nw"><button type="button" class="sec opbtn" title="გაშვება" data-op="start" data-c="${esc(r.id)}">▶</button><button type="button" class="sec opbtn" title="გაჩერება" data-op="stop" data-c="${esc(r.id)}">■</button><button type="button" class="sec opbtn" title="რესტარტი" data-op="restart" data-c="${esc(r.id)}">↻</button><br><button type="button" class="sec opbtn" title="ბოლო ლოგი" data-op="logs" data-c="${esc(r.id)}">ლოგი</button><button type="button" class="sec opbtn" title="ლოგი ცოცხლად" data-op="follow" data-c="${esc(r.id)}">ცოცხლად</button></td></tr>`;
  }).join("");
}

function setLive() {
  clearInterval(OPS_TIMER); OPS_TIMER = null;
  if ($("#st-live").checked) { refreshStatus(true); OPS_TIMER = setInterval(() => refreshStatus(true), 5000); }
}

async function streamJob(url, body, onDone) {
  const log = $("#log"); log.textContent = "";
  const {job} = await (await api(url, body)).json();
  let next = 0, done = false;
  while (!done) {
    const j = await (await fetch(`/api/jobs/${job}?since=${next}`)).json();
    if (j.lines.length) { log.textContent += j.lines.join("\n") + "\n"; log.scrollTop = log.scrollHeight; }
    next = j.next; done = j.done;
    if (done) log.textContent += j.ok ? "\n✔ დასრულდა\n" : "\n✘ დასრულდა შეცდომით/შეჩერდა\n";
    if (!done) await new Promise((r) => setTimeout(r, 600));
  }
  if (onDone) onDone();
}

async function opControl(action, comps) {
  showErr("");
  if (!opsOk()) return;
  const what = comps ? comps.join(", ") : "მთელი სტეკი";
  if ((action === "stop" || action === "restart") && !confirm(`${action}: ${what}\nგავაგრძელო?`)) return;
  try {
    await streamJob("/api/ops/control", opsBody({action, components: comps}));
    await refreshStatus(true);
  } catch (e) { showErr(e.message); }
}

async function opLogs(comp, follow) {
  showErr("");
  if (!opsOk()) return;
  await stopLogs();
  try {
    const log = $("#log"); log.textContent = "";
    const {job} = await (await api("/api/ops/logs", opsBody({component: comp, lines: follow ? 50 : 200, follow}))).json();
    LOG_JOB = job;
    $("#btn-log-stop").classList.toggle("nodisp", !follow);
    let next = 0, done = false;
    while (!done && LOG_JOB === job) {
      const j = await (await fetch(`/api/jobs/${job}?since=${next}`)).json();
      if (j.lines.length) { log.textContent += j.lines.join("\n") + "\n"; log.scrollTop = log.scrollHeight; }
      next = j.next; done = j.done;
      if (!done) await new Promise((r) => setTimeout(r, 800));
    }
    if (LOG_JOB === job) { LOG_JOB = null; $("#btn-log-stop").classList.add("nodisp"); }
  } catch (e) { showErr(e.message); }
}
async function stopLogs() {
  const job = LOG_JOB; LOG_JOB = null;
  $("#btn-log-stop").classList.add("nodisp");
  if (job) { try { await api(`/api/jobs/${job}/cancel`, {}); } catch (e) {} }
}

function initOps() {
  $("#btn-st-refresh").onclick = () => refreshStatus(false);
  $("#st-live").onchange = setLive;
  $("#btn-log-stop").onclick = stopLogs;
  document.querySelectorAll("button[data-all]").forEach((b) => b.onclick = () => opControl(b.dataset.all, null));
  $("#st-body").addEventListener("click", (ev) => {
    const b = ev.target.closest("button[data-op]"); if (!b) return;
    const c = b.dataset.c, op = b.dataset.op;
    if (op === "logs") return opLogs(c, false);
    if (op === "follow") return opLogs(c, true);
    return opControl(op, [c]);
  });
  $("#st-body").addEventListener("change", (ev) => {
    const cb = ev.target.closest("input.op-auto"); if (!cb) return;
    opControl(cb.checked ? "enable" : "disable", [cb.dataset.c]);
  });
}

function updMsg(cls, html) { const b = $("#updbox"); b.className = cls; b.style.display = "block"; b.innerHTML = html; }
async function checkUpdate() {
  updMsg("", "მოწმდება GitHub-ზე…");
  try {
    const j = await (await api("/api/update/check", {})).json();
    if (!j.newer) return updMsg("ok", `✔ უკვე უახლესი ვერსიაა (<b>${esc(j.current)}</b>). <a href="#" id="upd-close">დახურვა</a>`);
    const links = j.assets.map((a) => `<a class="dl" href="${esc(a.url)}" rel="noopener">⬇ ${esc(a.name)}</a>`).join("");
    const how = {
      venv: "ეს დაყენება შეიძლება განახლდეს ავტომატურად (ჩამოტვირთვა → SHA-256 შემოწმება → pip).",
      package: "პაკეტით დაყენებულია: ჩამოტვირთე შესაბამისი .deb/.rpm და დააყენე (apt install ./ფაილი / dnf install ./ფაილი).",
      source: "git checkout-ია: გაუშვი <code>git pull</code>.",
    }[j.install_kind] || "ჩამოტვირთე ახალი ვერსია ხელით.";
    updMsg("", `<b>ახალი ვერსია: ${esc(j.latest)}</b> (შენი: ${esc(j.current)}) ${j.published ? "· " + esc(j.published) : ""}
      <div class="note">${how}</div>
      ${j.notes ? `<pre>${esc(j.notes)}</pre>` : ""}
      <div>${links}<a class="dl bggray" href="${esc(j.url)}" target="_blank" rel="noopener">გამოშვების გვერდი</a>
      ${j.can_auto ? '<button type="button" id="upd-apply" class="go mt4">განახლება ახლავე</button>' : ""}
      <a href="#" id="upd-close" class="ml10">დახურვა</a></div>`);
    if ($("#upd-apply")) $("#upd-apply").onclick = applyUpdate;
  } catch (e) { updMsg("bad", `ვერ შევამოწმე: ${esc(e.message)} <a href="#" id="upd-close">დახურვა</a>`); }
}
async function applyUpdate() {
  if (!confirm("ახალი ვერსია ჩამოიტვირთება, შემოწმდება (SHA-256) და ჩანაცვლდება ამ პროგრამის ფაილები. გავაგრძელო?")) return;
  showErr("");
  try {
    await streamJob("/api/update/apply", {});
    updMsg("ok", "✔ განახლება დასრულდა (ლოგი მარჯვნივ). <b>გადატვირთე <code>gisstack web</code></b> ახალი ვერსიის გასაშვებად.");
  } catch (e) { showErr(e.message); }
}
document.addEventListener("click", (ev) => { if (ev.target && ev.target.id === "upd-close") { ev.preventDefault(); $("#updbox").style.display = "none"; } });

let EX = null;
const EX_LABEL = {ignore: "დავტოვო როგორც არის", adopt: "დაშენება (გამოვიყენოთ არსებული)", remove: "წაშლა (ჯერ ბექაფი)", reuse: "გამოყენება ჩვენში (ბექაფი → წაშლა → იმპორტი)", disable: "გავაჩერო და გავთიშო"};
function exOpts(opts) { return opts.map((o) => `<option value="${o}">${esc(EX_LABEL[o])}</option>`).join(""); }
function exFound(c) {
  const bits = [];
  if (c.version) bits.push(`<b>${esc(c.version)}</b>`);
  c.services.slice(0, 4).forEach((x) => bits.push(`service ${esc(x.name)}: ${esc(x.active)}`));
  c.packages.slice(0, 3).forEach((x) => bits.push(`pkg ${esc(x.name)}`));
  c.listen.forEach((x) => bits.push(`<code>${esc(x)}</code>`));
  c.data.forEach((x) => bits.push(`მონაცემები: ${esc(x)}`));
  c.details.forEach((x) => bits.push(`<span class="note">${esc(x)}</span>`));
  return bits.join("<br>") || "—";
}
function renderEx(rep) {
  EX = rep;
  const rows = [];
  Object.values(rep.components).filter((c) => c.found).forEach((c) => {
    const tag = c.ours && !c.foreign ? '<span class="tag tag-ours">ჩვენია</span>' : (c.foreign && !c.ours ? '<span class="tag tag-foreign">უცხო</span>' : '<span class="tag tag-both">ორივე</span>');
    let act = "<span class='note'>ჩვენი ინსტალაცია — არაფერს ვუშვებთ</span>";
    if (c.foreign) {
      const o = c.id === "postgis" ? ["ignore", "adopt", "remove", "reuse"] : ["ignore", "remove", "reuse"];
      act = `<select class="ex-act" data-c="${esc(c.id)}">${exOpts(o)}</select> <label><input type="checkbox" class="ex-purge" data-c="${esc(c.id)}"> მონაცემებიც წაიშალოს</label>`;
    }
    rows.push(`<tr><td><b>${esc(c.name)}</b><br>${tag}</td><td>${exFound(c)}</td><td>${act}</td></tr>`);
  });
  rep.web_servers.forEach((w) => {
    const active = w.services.some((x) => x.active === "active");
    rows.push(`<tr><td><b>${esc(w.name)}</b><br><span class="tag tag-foreign">სხვა web სერვერი</span></td><td>${w.services.map((x) => `service ${esc(x.name)}: ${esc(x.active)}`).join("<br>")}<br>${w.listen.map((x) => `<code>${esc(x)}</code>`).join("<br>")}</td>
      <td><select class="ex-act" data-c="${esc(w.name)}">${exOpts(active ? ["ignore", "disable"] : ["ignore"])}</select></td></tr>`);
  });
  $("#ex-body").innerHTML = rows.join("") || '<tr><td colspan="3">✔ არაფერი ნაპოვნია — სუფთა სერვერია.</td></tr>';
  const box = $("#ex-conf");
  if (rep.conflicts.length) {
    box.style.display = "block";
    box.innerHTML = "<b>⚠ პორტის კონფლიქტი</b> (Deploy ვერ გაეშვება, სანამ არ აირჩევ):<br>" +
      rep.conflicts.map((x) => `${esc(x.component)}: პორტი ${x.port} დაკავებულია — ${esc(x.process)} (${esc(x.addr)})`).join("<br>");
  } else box.style.display = "none";
  $("#ex-note").textContent = `${rep.os} · ${rep.conflicts.length ? "კონფლიქტები: " + rep.conflicts.length : "კონფლიქტები არ არის"}`;
}
function exPlan() {
  const plan = {};
  document.querySelectorAll("#ex-body select.ex-act").forEach((sel) => {
    if (sel.value === "ignore") return;
    const p = document.querySelector(`#ex-body input.ex-purge[data-c="${sel.dataset.c}"]`);
    plan[sel.dataset.c] = {action: sel.value, purge: !!(p && p.checked)};
  });
  return plan;
}
async function exScan() {
  showErr(""); showMsg("");
  if (!opsOk()) return;
  if ($("#deploy_target").value !== "native") return showErr("არსებული პროგრამების შემოწმება მხოლოდ native სამიზნეზეა (სექცია 7).");
  $("#ex-note").textContent = "მოწმდება…";
  try {
    const j = await (await api("/api/existing/scan", opsBody())).json();
    renderEx(j);
  } catch (e) { showErr(e.message); $("#ex-note").textContent = ""; }
}
async function exBackup() {
  showErr("");
  if (!opsOk()) return;
  const comps = EX ? Object.values(EX.components).filter((c) => c.foreign).map((c) => c.id) : [];
  if (!comps.length) return showErr("ჯერ დააჭირე „შემოწმებას“ — უცხო პროგრამა არ არის ნაპოვნი.");
  try { await streamJob("/api/existing/backup", opsBody({components: comps, with_data: $("#ex_data").checked})); } catch (e) { showErr(e.message); }
}
async function exRun() {
  showErr("");
  if (!opsOk()) return;
  const plan = exPlan();
  if (!Object.keys(plan).length) return showErr("გეგმა ცარიელია: აირჩიე მოქმედება მინიმუმ ერთ პროგრამაზე.");
  const what = Object.entries(plan).map(([k, v]) => `${k}: ${v.action}${v.purge ? " + მონაცემები" : ""}`).join("\n");
  const wipes = Object.values(plan).some((v) => v.purge && (v.action === "remove" || v.action === "reuse"));
  if (!confirm(`შესრულდება (ყოველ წაშლამდე ბექაფი):\n${what}\n\n` + (wipes ? "⚠ მონაცემებიც წაიშლება და ვერ აღდგება!\n\n" : "") + "გავაგრძელო?")) return;
  try {
    await streamJob("/api/existing/execute", opsBody({plan, install: false, with_data: $("#ex_data").checked, confirm_data_loss: wipes}));
    await exScan();
  } catch (e) { showErr(e.message); }
}

// ---------------------------------------------------------------- i18n (Georgian is the source text)
const KA_RE = /[Ⴀ-ჿ]/;
let EN = {}, EN_RE = null, LANG = "ka", SETTINGS = {lang: "auto", theme: "auto"};
const ORIG_TXT = new WeakMap(), ORIG_ATTR = new WeakMap();
let SET_TXT = new WeakMap(), FORCE_ATTR = false;   // reset on every language change so all text is re-translated
const ATTRS = ["placeholder", "title", "aria-label"];
function buildEn(tbl) {
  EN = tbl;
  const keys = Object.keys(EN).sort((a, b) => b.length - a.length).map((k) => k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  EN_RE = new RegExp("(?<![Ⴀ-ჿ])(?:" + keys.join("|") + ")(?![Ⴀ-ჿ])", "g");
}
function trText(s) {
  if (!s || !KA_RE.test(s)) return s;
  const core = s.trim();
  if (Object.prototype.hasOwnProperty.call(EN, core)) return s.replace(core, () => EN[core]);
  return s.replace(EN_RE, (m) => EN[m]);
}
function skipNode(n) {   // never translate user data: the file preview, script/style, form values
  const p = n.nodeType === 3 ? n.parentNode : n;
  return !p || p.closest && p.closest("script,style,#view,#filesel,textarea");
}
function applyText(n) {
  if (skipNode(n)) return;
  const cur = n.nodeValue;
  if (SET_TXT.get(n) === cur) return;                       // our own translation, nothing new
  let orig = ORIG_TXT.get(n);
  if (orig === undefined || (KA_RE.test(cur) && cur !== orig)) { if (KA_RE.test(cur)) { orig = cur; ORIG_TXT.set(n, orig); } }
  if (orig === undefined) return;
  const out = LANG === "en" ? trText(orig) : orig;
  if (out !== cur) { n.nodeValue = out; }
  SET_TXT.set(n, out);
}
function applyAttrs(el) {
  if (!el.getAttribute || skipNode(el)) return;
  let rec = ORIG_ATTR.get(el);
  for (const a of ATTRS) {
    if (!el.hasAttribute(a)) continue;
    const cur = el.getAttribute(a);
    if (!FORCE_ATTR && rec && rec[a] && rec[a].set === cur) continue;
    if (KA_RE.test(cur)) { rec = rec || {}; rec[a] = {orig: cur, set: null}; ORIG_ATTR.set(el, rec); }
    if (!rec || !rec[a]) continue;
    const out = LANG === "en" ? trText(rec[a].orig) : rec[a].orig;
    rec[a].set = out;
    if (out !== cur) el.setAttribute(a, out);
  }
}
function walk(root) {
  if (root.nodeType === 3) return applyText(root);
  if (root.nodeType !== 1) return;
  applyAttrs(root);
  const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
  let n; while ((n = w.nextNode())) { if (n.nodeType === 3) applyText(n); else applyAttrs(n); }
}
let OBS = null;
function startObserver() {
  if (OBS) return;
  OBS = new MutationObserver((muts) => {
    for (const m of muts) {
      if (m.type === "characterData") applyText(m.target);
      else if (m.type === "attributes") applyAttrs(m.target);
      else m.addedNodes.forEach(walk);
    }
  });
  OBS.observe(document.body, {childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ATTRS});
}
const _confirm = window.confirm.bind(window), _alert = window.alert.bind(window);
window.confirm = (m) => _confirm(LANG === "en" ? trText(String(m)) : m);
window.alert = (m) => _alert(LANG === "en" ? trText(String(m)) : m);

// ---------------------------------------------------------------- settings: language + theme
function resolvedLang(v) { return v === "ka" || v === "en" ? v : ((navigator.language || "").toLowerCase().startsWith("ka") ? "ka" : "en"); }
function resolvedTheme(v) { return v === "light" || v === "dark" ? v : (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"); }
function applySettings() {
  LANG = resolvedLang(SETTINGS.lang);
  document.documentElement.setAttribute("data-theme", resolvedTheme(SETTINGS.theme));
  document.documentElement.lang = LANG;
  try { localStorage.setItem("gs_theme", SETTINGS.theme); localStorage.setItem("gs_lang", SETTINGS.lang); } catch (e) {}
  document.querySelectorAll("#langseg button").forEach((b) => b.classList.toggle("on", b.dataset.lang === LANG));
  $("#set-lang").value = SETTINGS.lang; $("#set-theme").value = SETTINGS.theme;
  SET_TXT = new WeakMap(); FORCE_ATTR = true;
  try { walk(document.body); } finally { FORCE_ATTR = false; }
}
async function saveSettings(patch) {
  SETTINGS = Object.assign({}, SETTINGS, patch);
  applySettings();
  try { await api("/api/settings", patch); } catch (e) { /* the choice still applies for this session */ }
}
function initSettings() {
  document.querySelectorAll("#langseg button").forEach((b) => b.onclick = () => saveSettings({lang: b.dataset.lang}));
  $("#btn-theme").onclick = () => { const next = {auto: "light", light: "dark", dark: "auto"}[SETTINGS.theme] || "auto"; saveSettings({theme: next}); };
  $("#btn-set").onclick = () => $("#dlg-set").showModal();
  $("#set-close").onclick = () => $("#dlg-set").close();
  $("#set-lang").onchange = () => saveSettings({lang: $("#set-lang").value});
  $("#set-theme").onchange = () => saveSettings({theme: $("#set-theme").value});
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (SETTINGS.theme === "auto") applySettings(); });
}
async function bootI18n() {
  try {
    const [st, tb] = await Promise.all([fetch("/api/settings").then((r) => r.json()), fetch("/api/i18n").then((r) => r.json())]);
    SETTINGS = {lang: st.lang, theme: st.theme}; buildEn(tb.en);
  } catch (e) { buildEn({}); }
  initSettings();
  applySettings();
  startObserver();
  document.documentElement.classList.add("ready");
}

(async function init() {
  await bootI18n();
  const j = await (await fetch("/api/catalog")).json();
  CAT = j.catalog; DEF = j.defaults;
  fill(DEF);
  cleanRows();
  buildBars();
  initOps();
  $("#btn-ex-scan").onclick = exScan; $("#btn-ex-backup").onclick = exBackup; $("#btn-ex-run").onclick = exRun;
  $("#btn-upd").onclick = checkUpdate;
  await loadProjects();
  $("#btn-proj-load").onclick = loadProject; $("#btn-proj-save").onclick = saveProject;
  $("#rootnote").textContent = j.is_root ? "" : "გაფრთხილება: ეს პროცესი root-ით არ მუშაობს — „ამ მანქანაზე“ native ინსტალაცია არ იმუშავებს.";
  $("#addlayer").onclick = () => addLayer();
  $("#mode").onchange = () => { $("#sshbox").style.display = $("#mode").value === "ssh" ? "flex" : "none"; };
  $("#btn-preview").onclick = preview; $("#btn-zip").onclick = zip; $("#btn-deploy").onclick = deploy; $("#btn-clean-all").onclick = () => cleanup(null);
})();

document.getElementById("form").addEventListener("submit", (e) => e.preventDefault());   // was an inline onsubmit handler (blocked by the strict CSP)
