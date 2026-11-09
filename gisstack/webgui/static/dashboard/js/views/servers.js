// Servers: saved profiles with an environment tag, the PATH of an SSH key (never its content), a pinned host key, health, comparison and history.
// Every other page can then work on a chosen server: only its id is sent, the credentials and the pinned key stay in the profile.
import { get, put, post, request } from "../api.js";
import { h, append, clear, toast, loading, done, errorBox, pill, table, kv, modal, confirmBox, typedConfirm } from "../ui.js";
import * as state from "../state.js";

const ENV = { production: "პროდაქშენი", development: "დეველოპმენტი", testing: "ტესტირება" };
const ENV_KIND = { production: "bad", development: "ok", testing: "warn" };
const field = (id, label, input) => h("div", { class: "field" }, h("label", { for: id }, label), input);

export default {
  title: () => "სერვერები",
  async mount(root) {
    const body = h("div", { class: "stack" });
    const picked = new Set();
    append(root, [h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "სერვერები"), h("p", { class: "muted" }, "ყოველ სერვერს თავისი მისამართი, გასაღების გზა და დაფიქსირებული host key აქვს. გასაღების შიგთავსი არსად ინახება."))), body]);

    async function refresh() {
      clear(body); loading(body, 3);
      let list;
      try { list = await get("/api/v1/servers"); } catch (e) { done(body); clear(body); errorBox(body, e, refresh); return; }
      done(body); clear(body);
      const active = state.getConn().server;
      append(body, [
        h("section", { class: "card stack" }, h("h2", {}, "პროფილები"),
          table(["", "სახელი", "გარემო", "მისამართი", "host key", ""], list.map((s) => [
            h("input", { type: "checkbox", "aria-label": s.id, checked: picked.has(s.id) ? "" : null, onchange: (ev) => { if (ev.target.checked) picked.add(s.id); else picked.delete(s.id); } }),
            h("span", {}, s.name || s.id, active === s.id ? "  ✔" : ""),
            pill(ENV_KIND[s.env] || "", ENV[s.env] || s.env),
            `${s.user}@${s.host}:${s.port}`,
            s.pinned ? pill("ok", "დაფიქსირებულია") : pill("warn", "არ არის დაფიქსირებული"),
            h("div", { class: "row" },
              h("button", { type: "button", class: "btn", onclick: () => probe(s) }, "შემოწმება"),
              h("button", { type: "button", class: "btn", onclick: () => pin(s) }, s.pinned ? "host key-ის შეცვლა" : "host key-ის დაფიქსირება"),
              h("button", { type: "button", class: "btn", onclick: () => history(s) }, "ისტორია"),
              h("button", { type: "button", class: "btn", onclick: () => edit(s) }, "რედაქტირება"),
              s.pinned ? h("button", { type: "button", class: "btn primary", onclick: () => use(s) }, "გამოყენება") : null,
              h("button", { type: "button", class: "btn danger", onclick: () => remove(s) }, "წაშლა"))]), { empty: "სერვერი ჯერ არ არის დამატებული" }),
          h("div", { class: "row" },
            h("button", { type: "button", class: "btn primary", onclick: () => edit(null) }, "სერვერის დამატება"),
            h("button", { type: "button", class: "btn", onclick: () => compare(list) }, "მონიშნულების შედარება"),
            active ? h("button", { type: "button", class: "btn", onclick: () => state.setConn({ ...state.getConn(), server: "" }).then(refresh) }, "ამ კომპიუტერზე დაბრუნება") : null)),
        h("section", { class: "card stack", id: "srv-out" }),
      ]);
    }
    const out = () => document.getElementById("srv-out");
    const show = (title, node) => { const o = out(); clear(o); append(o, [h("h2", {}, title), node]); o.scrollIntoView({ block: "nearest" }); };

    async function edit(s) {
      const v = Object.assign({ id: "", name: "", env: "development", host: "", port: 22, user: "root", key: "", notes: "" }, s || {});
      const f = {
        id: h("input", { id: "sv-id", value: v.id, disabled: s ? "" : null, placeholder: "prod1" }), name: h("input", { id: "sv-name", value: v.name }),
        env: h("select", { id: "sv-env" }, ...Object.keys(ENV).map((k) => h("option", { value: k, selected: k === v.env ? "" : null }, ENV[k]))),
        host: h("input", { id: "sv-host", value: v.host }), port: h("input", { id: "sv-port", type: "number", value: String(v.port) }), user: h("input", { id: "sv-user", value: v.user }),
        key: h("input", { id: "sv-key", value: v.key, placeholder: "/home/me/.ssh/id_ed25519" }), notes: h("input", { id: "sv-notes", value: v.notes || "" }),
      };
      const ok = await modal({ title: s ? "სერვერის რედაქტირება" : "სერვერის დამატება", actions: [{ label: "გაუქმება", value: false }, { label: "შენახვა", value: true, kind: "primary" }],
        body: h("div", { class: "stack" }, field("sv-id", "id", f.id), field("sv-name", "სახელი", f.name), field("sv-env", "გარემო", f.env), field("sv-host", "მისამართი", f.host), field("sv-port", "პორტი", f.port),
          field("sv-user", "მომხმარებელი", f.user), field("sv-key", "SSH გასაღების გზა (ცარიელი: ssh-agent)", f.key), field("sv-notes", "შენიშვნა", f.notes),
          h("p", { class: "muted" }, "ჩაწერეთ მხოლოდ ფაილის გზა. გასაღების შიგთავსი არ მიიღება და არ ინახება. მისამართის შეცვლა host key-ს აუქმებს.")) });
      if (ok !== true) return;
      try {
        await put("/api/v1/servers/" + encodeURIComponent(f.id.value.trim()), { name: f.name.value, env: f.env.value, host: f.host.value.trim(), port: Number(f.port.value) || 22, user: f.user.value.trim(), key: f.key.value.trim(), notes: f.notes.value });
        toast("შენახულია", "ok"); refresh();
      } catch (e) { toast(e.message, "bad"); }
    }

    async function pin(s) {
      let keys;
      try { keys = await post(`/api/v1/servers/${encodeURIComponent(s.id)}/hostkey/scan`, {}); } catch (e) { toast(e.message, "bad"); return; }
      const fp = h("input", { id: "sv-fp", autocomplete: "off", placeholder: "SHA256:..." });
      const ok = await modal({ title: `host key — ${s.id}`, actions: [{ label: "გაუქმება", value: false }, { label: "დაფიქსირება", value: true, kind: "primary" }],
        body: h("div", { class: "stack" },
          h("p", {}, "სერვერმა ეს გასაღებები აჩვენა. შეადარეთ ანაბეჭდი სერვერის კონსოლს (ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub) და ჩაწერეთ აქ - ავტომატურად არაფერი დაჯერდება."),
          table(["ტიპი", "ანაბეჭდი"], keys.keys.map((k) => [k.type, k.fingerprint])),
          keys.pinned.length ? h("p", { class: "muted" }, "ამჟამად დაფიქსირებული: " + keys.pinned.join(", ")) : null,
          field("sv-fp", "ანაბეჭდი (fingerprint)", fp)) });
      if (ok !== true) return;
      try {
        if (s.pinned && !(await typedConfirm("host key შეიცვალა? ეს ნიშნავს ან სერვერი თავიდან აიგო, ან ვიღაც ერევა შუაში.", "REPLACE", { title: "host key-ის ჩანაცვლება" }))) return;
        await post(`/api/v1/servers/${encodeURIComponent(s.id)}/hostkey/pin`, { fingerprint: fp.value.trim(), replace: !!s.pinned });
        toast("host key დაფიქსირდა", "ok"); refresh();
      } catch (e) { toast(e.message, "bad"); }
    }

    async function probe(s) {
      show(`შემოწმება — ${s.id}`, h("p", { class: "muted" }, "…"));
      try {
        const r = await post(`/api/v1/servers/${encodeURIComponent(s.id)}/probe`, {});
        show(`შემოწმება — ${s.id}`, r.reachable
          ? h("div", {}, pill("ok", "✔ მისაწვდომია"), kv([["OS", r.facts.os], ["kernel", r.facts.kernel], ["sudo", r.facts.root], ["load", r.facts.load], ["თავისუფალი დისკი (kB)", r.facts.disk_free_kb], ["დრო (წმ)", String(r.seconds)]]))
          : h("div", {}, pill("bad", "✖ მიუწვდომელია"), h("p", {}, r.error)));
      } catch (e) { show(`შემოწმება — ${s.id}`, h("p", {}, e.message)); }
    }

    async function compare(list) {
      const ids = [...picked].filter((id) => list.some((s) => s.id === id));
      if (ids.length < 2) { toast("მონიშნეთ მინიმუმ ორი სერვერი", "warn"); return; }
      show("შედარება", h("p", { class: "muted" }, "…"));
      try {
        const r = await post("/api/v1/servers/compare", { ids });
        show("შედარება", h("div", { class: "stack" },
          h("p", {}, r.drift ? pill("warn", `განსხვავება: ${r.drift}`) : pill("ok", "განსხვავება არ არის"), " ", r.complete ? null : pill("bad", "ზოგი სერვერი მიუწვდომელია")),
          table(["", ...ids], r.rows.map((row) => [row.differs ? "≠ " + row.fact : row.fact, ...ids.map((id) => row.values[id] === undefined ? "—" : row.fact.startsWith("sha.") ? row.values[id].slice(0, 12) : row.values[id])]), { empty: "მონაცემი არ არის" }),
          r.servers.filter((x) => !x.reachable).length ? table(["სერვერი", "შეცდომა"], r.servers.filter((x) => !x.reachable).map((x) => [x.id, x.error])) : null));
      } catch (e) { show("შედარება", h("p", {}, e.message)); }
    }

    async function history(s) {
      try {
        const rows = await get(`/api/v1/servers/${encodeURIComponent(s.id)}/history`);
        show(`ისტორია — ${s.id}`, table(["დრო", "ოპერაცია", "პროექტი", "შედეგი", "წმ"], rows.map((r) => [r.started ? new Date(r.started * 1000).toLocaleString() : "", r.op, r.project, r.status, r.seconds === null ? "" : String(r.seconds)]), { empty: "ამ სერვერზე ოპერაცია ჯერ არ ყოფილა" }));
      } catch (e) { show(`ისტორია — ${s.id}`, h("p", {}, e.message)); }
    }

    async function use(s) {
      await state.setConn({ ...state.getConn(), mode: "ssh", server: s.id, ssh: { host: s.host, port: s.port, user: s.user, key: s.key } });
      toast(`${s.id} არჩეულია`, "ok"); refresh();
    }

    async function remove(s) {
      if (!(await confirmBox(`წაიშლება პროფილი და დაფიქსირებული host key (${s.id}). თავად სერვერს არაფერი მოუვა.`, { danger: true, ok: "წაშლა" }))) return;
      try { await request("DELETE", "/api/v1/servers/" + encodeURIComponent(s.id)); if (state.getConn().server === s.id) await state.setConn({ ...state.getConn(), server: "" }); picked.delete(s.id); refresh(); } catch (e) { toast(e.message, "bad"); }
    }
    refresh();
  },
};
