// Logs: pick a program, read the last lines or follow live. Uses the same job endpoints as the classic page; "Stop" cancels the remote tail.
import { post, runJob } from "../api.js";
import { h, append, clear, toast } from "../ui.js";
import * as state from "../state.js";
import { t } from "../i18n.js";

const COMPS = [["postgis", "PostgreSQL"], ["tomcat", "Tomcat"], ["geoserver", "GeoServer"], ["qgis_server", "QGIS Server"], ["mapproxy", "MapProxy"], ["caddy", "Caddy"]];
const MAX_CHARS = 400000;           // keep the page responsive: only the newest part of a long follow stays in the DOM

export default {
  title: () => "ლოგები",
  async mount(root, { params }) {
    const cfg = state.getConfig();
    const enabled = COMPS.filter(([id]) => !cfg.components || cfg.components[id] || (id === "tomcat" && cfg.components.geoserver));
    const sel = h("select", { id: "lg-comp" }, ...enabled.map(([id, l]) => h("option", { value: id, selected: id === (params.c || enabled[0][0]) }, l)));
    const lines = h("select", { id: "lg-lines" }, ...[100, 200, 500, 1000].map((n) => h("option", { value: n, selected: n === 200 }, String(n))));
    const follow = h("input", { type: "checkbox", id: "lg-follow" });
    const filter = h("input", { type: "search", id: "lg-filter", placeholder: "ფილტრი…", "aria-label": "ფილტრი" });
    const out = h("pre", { class: "log", role: "log", "aria-label": "ლოგი", tabindex: "0" });
    const go = h("button", { type: "button", class: "btn primary", onclick: start }, "ჩატვირთვა");
    const stop = h("button", { type: "button", class: "btn", onclick: halt, disabled: true }, "გაჩერება");
    const save = h("button", { type: "button", class: "btn", onclick: download }, "ფაილად შენახვა");
    let ctl = null, all = [];
    append(root, [h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "ლოგები"), h("p", { class: "muted" }, state.describe()))),
      h("div", { class: "card stack" }, h("div", { class: "row" },
        h("div", {}, h("label", { for: "lg-comp" }, "პროგრამა"), sel), h("div", {}, h("label", { for: "lg-lines" }, "ხაზები"), lines),
        h("label", { class: "check", for: "lg-follow" }, follow, "რეალურ დროში"), h("div", {}, h("label", { for: "lg-filter" }, "ფილტრი"), filter), go, stop, save), out)]);

    const shown = () => { const f = filter.value.toLowerCase(); return (f ? all.filter((l) => l.toLowerCase().includes(f)) : all).join("\n"); };
    const paint = () => { const atEnd = out.scrollTop + out.clientHeight >= out.scrollHeight - 30; out.textContent = shown().slice(-MAX_CHARS); if (atEnd) out.scrollTop = out.scrollHeight; };
    filter.addEventListener("input", paint);
    function halt() { if (ctl) ctl.abort(); }
    async function start() {
      halt();
      all = []; out.textContent = "";
      ctl = new AbortController();
      go.disabled = true; stop.disabled = false;
      try {
        await runJob("/api/ops/logs", state.ctx({ component: sel.value, lines: Number(lines.value), follow: follow.checked }), { signal: ctl.signal, interval: 700, onLines: (l) => { all.push(...l); if (all.length > 5000) all = all.slice(-5000); paint(); } });
      } catch (e) { toast(e.message, "bad"); }
      go.disabled = false; stop.disabled = true;
    }
    function download() {
      const blob = new Blob([all.join("\n") + "\n"], { type: "text/plain" });
      const a = h("a", { href: URL.createObjectURL(blob), download: `${sel.value}.log` });
      document.body.appendChild(a); a.click(); a.remove();
    }
    await start();
    return { destroy: halt };
  },
};
