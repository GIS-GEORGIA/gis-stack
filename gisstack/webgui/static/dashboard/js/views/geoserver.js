// GeoServer manager: health, inventory (workspaces, layers, groups, styles), capabilities, layer preview and workspace changes.
// All calls go through the /api/v1/geoserver routes; the server talks to GeoServer with the stack's own credentials (never shown here).
import { post, postImage } from "../api.js";
import { h, append, clear, toast, loading, done, errorBox, pill, table, kv, typedConfirm, modal } from "../ui.js";
import * as state from "../state.js";

const call = (path, extra) => post(path, state.ctx(extra));

export default {
  title: () => "GeoServer-ის მენეჯერი",
  async mount(root) {
    const body = h("div", { class: "stack" });
    let urls = [];
    append(root, [h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "GeoServer-ის მენეჯერი"), h("p", { class: "muted" }, state.describe()))), body]);

    async function refresh() {
      clear(body); loading(body, 4);
      let health, inv, issues;
      try { [health, inv, issues] = await Promise.all([call("/api/v1/geoserver/health"), call("/api/v1/geoserver/inventory"), call("/api/v1/geoserver/validate")]); }
      catch (e) { done(body); clear(body); errorBox(body, e, refresh); return; }
      done(body); clear(body);
      const ver = health.version.GeoServer || "—";
      append(body, [
        h("section", { class: "card" }, h("h2", {}, "მდგომარეობა"),
          h("p", {}, pill(health.ok ? "ok" : "bad", health.ok ? "✔ მუშაობს" : "✖ პრობლემაა"), " ", `GeoServer ${ver}`),
          table(["შემოწმება", "შედეგი", "დეტალი"], health.checks.map((c) => [c.check, pill(c.ok ? "ok" : "bad", c.ok ? "✔ OK" : "✖ შეცდომა"), c.detail || ""]))),
        issues.length ? h("section", { class: "card" }, h("h2", {}, "ნაპოვნი საკითხები"),
          table(["დონე", "რა", "შეტყობინება"], issues.map((i) => [i.level, i.what, i.message]))) : null,
        workspacesCard(inv), layersCard(inv),
        h("section", { class: "card" }, h("h2", {}, "ფენების ჯგუფები და სტილები"),
          kv([["ფენების ჯგუფები", inv.layergroups.map((g) => g.name).join(", ") || "—"], ["სტილები", inv.styles.map((s) => s.name).join(", ") || "—"]])),
        capabilitiesCard(),
      ]);
      Object.entries(inv.errors).forEach(([k, m]) => toast(`${k}: ${m}`, "bad"));
    }

    function workspacesCard(inv) {
      const name = h("input", { type: "text", id: "ws-name", placeholder: "workspace", pattern: "[A-Za-z0-9_][A-Za-z0-9_.-]*", maxlength: "63", "aria-label": "workspace" });
      const add = h("button", { type: "button", class: "btn primary", onclick: async () => {
        try { await call("/api/v1/geoserver/workspaces", { name: name.value.trim() }); toast("შეიქმნა", "ok"); await refresh(); } catch (e) { toast(e.message, "bad"); }
      } }, "შექმნა");
      const rows = inv.workspaces.map((w) => [w.name, String(inv.datastores.filter((d) => d.workspace === w.name).length),
        h("button", { type: "button", class: "btn danger", onclick: () => removeWs(w.name) }, "წაშლა")]);
      return h("section", { class: "card stack" }, h("h2", {}, "Workspace-ები"),
        table(["სახელი", "მონაცემთა საცავები", ""], rows, { empty: "workspace არ არის" }), h("div", { class: "row" }, name, add));
    }
    async function removeWs(name) {
      if (!(await typedConfirm(`Workspace „${name}“ და მასში არსებული ყველა ფენა და საცავი წაიშლება. ამის გაუქმება შეუძლებელია.`, name, { ok: "წაშლა" }))) return;
      try { await call("/api/v1/geoserver/workspaces/delete", { name, recurse: true, confirm: true }); toast("წაიშალა", "ok"); await refresh(); } catch (e) { toast(e.message, "bad"); }
    }

    function layersCard(inv) {
      const rows = inv.layers.map((l) => [l.name, l.workspace, h("button", { type: "button", class: "btn", onclick: () => show(l.name) }, "გადახედვა")]);
      return h("section", { class: "card" }, h("h2", {}, "ფენები"), table(["ფენა", "Workspace", ""], rows, { empty: "ფენა ჯერ არ არის გამოქვეყნებული" }));
    }
    async function show(name) {
      const img = h("img", { alt: name, width: "512", height: "256", class: "preview" });
      const status = h("p", { class: "muted" }, "იტვირთება…");
      const m = modal({ title: name, body: h("div", { class: "stack" }, status, img), actions: [{ label: "დახურვა", value: true, kind: "primary" }] });
      try {
        const u = await postImage("/api/v1/geoserver/preview", state.ctx({ layers: name, bbox: "-180,-90,180,90", width: 512, height: 256 }));
        urls.push(u); img.src = u; status.textContent = "";
      } catch (e) { status.textContent = e.message; }
      return m;
    }

    function capabilitiesCard() {
      const out = h("div", {});
      const buttons = ["WMS", "WFS", "WMTS"].map((svc) => h("button", { type: "button", class: "btn", onclick: async () => {
        clear(out); loading(out, 2);
        try {
          const c = await call("/api/v1/geoserver/capabilities", { service: svc });
          done(out); clear(out);
          append(out, [h("p", { class: "muted" }, `${svc} ${c.version}`), table(["ფენა", "სათაური"], c.layers.map((l) => [l.name, l.title]), { empty: "ფენა არ არის" })]);
        } catch (e) { done(out); clear(out); errorBox(out, e); }
      } }, svc));
      return h("section", { class: "card stack" }, h("h2", {}, "OGC სერვისები"), h("div", { class: "row" }, ...buttons), out);
    }

    await refresh();
    return { refresh, destroy() { urls.forEach((u) => URL.revokeObjectURL(u)); urls = []; } };
  },
};
