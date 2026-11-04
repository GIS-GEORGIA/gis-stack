// Pure-logic tests for the dashboard (node --test tests/js). No DOM needed: these modules do not touch `document` at import time.
import test from "node:test";
import assert from "node:assert/strict";
import { bytes, pct, duration, ago, seconds } from "../../gisstack/webgui/static/dashboard/js/format.js";
import { createTranslator } from "../../gisstack/webgui/static/dashboard/js/i18n.js";
import { niceTicks, timeTicks, scaleLinear, downsample, buildPath, nearest } from "../../gisstack/webgui/static/dashboard/js/chart.js";
import { parse } from "../../gisstack/webgui/static/dashboard/js/router.js";
import { groupSeries } from "../../gisstack/webgui/static/dashboard/js/views/monitoring.js";
import { configSummary } from "../../gisstack/webgui/static/dashboard/js/views/service.js";
import { summarize } from "../../gisstack/webgui/static/dashboard/js/views/overview.js";

test("bytes / pct / duration / seconds", () => {
  assert.equal(bytes(0), "0 B");
  assert.equal(bytes(1536), "1.5 KB");
  assert.equal(bytes(5 * 1024 ** 3), "5.0 GB");
  assert.equal(bytes(700600 * 1024), "684 MB");
  assert.equal(bytes(null), "-");
  assert.equal(bytes(undefined), "-");
  assert.equal(pct(12.345, 1), "12.3 %");
  assert.equal(pct(null), "-");
  assert.equal(duration(30), "30 წმ");
  assert.equal(duration(3700), "1 სთ 1 წთ");
  assert.equal(duration(90000), "1 დღე 1 სთ");
  assert.equal(duration(null), "-");
  assert.equal(ago(100, 102), "ახლახან");
  assert.equal(ago(0, 7200), "2 სთ 0 წთ წინ");
  assert.equal(seconds(0.1234), "0.123 s");
  assert.equal(seconds(12.34), "12.3 s");
});

test("translator: whole string first, then fragments on Georgian word boundaries; non-Georgian text untouched", () => {
  const tr = createTranslator({ "ბექაფი": "Backup", "ბექაფი: postgis": "Backup of postgis", "ახალი": "New", "სერვისი": "Service" });
  assert.equal(tr("ბექაფი"), "Backup");
  assert.equal(tr("  ბექაფი  "), "  Backup  ");
  assert.equal(tr("ახალი ბექაფი"), "New Backup");
  assert.equal(tr("ახალი სერვისები"), "New სერვისები");                // "სერვისები" is a different word: no partial match
  assert.equal(tr("plain text 123"), "plain text 123");
  assert.equal(tr(""), "");
  assert.equal(tr(null), null);
  assert.equal(createTranslator({})("ქართული"), "ქართული");
  const esc = createTranslator({ "a.b": "x" });
  assert.equal(esc("ქართული a.b"), "ქართული x");
  assert.equal(esc("ქართული axb"), "ქართული axb");              // regex metacharacters in keys are escaped, "." is not a wildcard
});

test("niceTicks / timeTicks / scaleLinear", () => {
  assert.deepEqual(niceTicks(0, 100, 4), [0, 25, 50, 75, 100]);
  assert.deepEqual(niceTicks(0, 1, 5), [0, 0.2, 0.4, 0.6, 0.8, 1]);
  const flat = niceTicks(5, 5, 4);
  assert.ok(flat[0] < 5 && flat[flat.length - 1] > 5);
  assert.deepEqual(niceTicks(NaN, 1), [0, 1]);
  const t = timeTicks(0, 3600, 5);
  assert.ok(t.length >= 1 && t.length <= 8);
  assert.ok(t.every((v, i) => i === 0 || v > t[i - 1]));
  const s = scaleLinear(0, 10, 100, 200);
  assert.equal(s(0), 100); assert.equal(s(10), 200); assert.equal(s(5), 150);
  assert.equal(scaleLinear(3, 3, 0, 10)(3), 5);
});

test("downsample keeps the shape and never invents points", () => {
  const pts = Array.from({ length: 1000 }, (_, i) => ({ ts: i, value: i % 10 }));
  const d = downsample(pts, 100);
  assert.ok(d.length <= 100 && d.length >= 90);
  assert.ok(d.every((p) => p.value >= 0 && p.value <= 9));
  assert.deepEqual(downsample(pts.slice(0, 5), 100), pts.slice(0, 5));
});

test("buildPath breaks the line where the collector was silent (a gap is not drawn through)", () => {
  const sx = (v) => v, sy = (v) => v;
  const steady = [0, 30, 60, 90].map((ts) => ({ ts, value: 1 }));
  assert.equal((buildPath(steady, sx, sy).match(/M/g) || []).length, 1);
  const gap = [0, 30, 60, 3000, 3030].map((ts) => ({ ts, value: 1 }));
  assert.equal((buildPath(gap, sx, sy).match(/M/g) || []).length, 2);
  assert.equal(buildPath([], sx, sy), "");
  assert.match(buildPath([{ ts: 1, value: 2 }], sx, sy), /^M1\.0 2\.0$/);
});

test("nearest", () => {
  const pts = [{ ts: 0, value: 1 }, { ts: 10, value: 2 }, { ts: 20, value: 3 }];
  assert.equal(nearest(pts, 12).value, 2);
  assert.equal(nearest(pts, 1000).value, 3);
  assert.equal(nearest([], 5), null);
});

test("router.parse", () => {
  assert.deepEqual(parse(""), { name: "overview", arg: null, params: {} });
  assert.deepEqual(parse("#/infra"), { name: "infra", arg: null, params: {} });
  assert.deepEqual(parse("#/service/geoserver"), { name: "service", arg: "geoserver", params: {} });
  assert.deepEqual(parse("#/logs?c=caddy&x=1"), { name: "logs", arg: null, params: { c: "caddy", x: "1" } });
});

test("groupSeries groups by label value and cycles colours", () => {
  const pts = [{ ts: 1, labels: { mount: "/" }, value: 1 }, { ts: 1, labels: { mount: "/var" }, value: 2 }, { ts: 2, labels: { mount: "/" }, value: 3 }];
  const g = groupSeries(pts, "mount");
  assert.deepEqual(g.map((s) => [s.name, s.points.length, s.cls]), [["/", 2, "c1"], ["/var", 1, "c2"]]);
  assert.equal(groupSeries(pts, null).length, 1);
  assert.equal(groupSeries(Array.from({ length: 8 }, (_, i) => ({ ts: i, labels: { k: String(i) }, value: i })), "k")[7].cls, "c2");
  assert.deepEqual(groupSeries([], "x"), []);
});

test("configSummary never exposes secrets and tolerates missing sections", () => {
  const cfg = { postgres: { version: "18", db: "gis", user: "gis", password: "TOPSECRET", port: 5432, extensions: ["postgis", "pgrouting"] },
    geoserver: { channel: "3", version: "3.0.2", admin_password: "TOPSECRET2", extensions: [] }, tomcat: { port: 8080, manager_password: "TOPSECRET3" } };
  for (const id of ["postgis", "geoserver", "qgis_server", "mapproxy", "webmap", "caddy"]) {
    const text = JSON.stringify(configSummary(id, cfg));
    assert.ok(!/TOPSECRET/.test(text), id);
    configSummary(id, {});
    configSummary(id, undefined);
  }
  assert.deepEqual(configSummary("postgis", cfg).find(([k]) => k === "გაფართოებები")[1], "postgis, pgrouting");
  assert.deepEqual(configSummary("nothing", cfg), []);
});

test("overview summarize: healthy only when everything is really fine", () => {
  const row = (state, ok = true) => ({ id: state, name: state, state, ok });
  assert.equal(summarize({ rows: [row("running"), row("running")] }, { alerts: [] }).healthy, true);
  assert.equal(summarize({ rows: [row("running"), row("failed", false)] }, { alerts: [] }).healthy, false);
  assert.equal(summarize({ rows: [row("running", false)] }, null).failing.length, 1);
  assert.equal(summarize({ rows: [row("running")] }, { alerts: [{ rule: "x" }] }).healthy, false);
  assert.equal(summarize(null, null).healthy, false);                          // no data is never "healthy"
  assert.equal(summarize({ rows: [] }, null).healthy, true);
});
