"""Human-readable API reference generated from the same route table (docs/API.md) - never edited by hand."""
from __future__ import annotations

import json
from typing import Any


def _fields(schema: dict[str, Any], components: dict[str, Any], indent: str = "") -> list[str]:
    if "$ref" in schema:
        schema = components.get(schema["$ref"].rsplit("/", 1)[-1], {})
    out = []
    req = set(schema.get("required", []))
    for name, s in schema.get("properties", {}).items():
        if "$ref" in s:
            s = components.get(s["$ref"].rsplit("/", 1)[-1], s)
        t = s.get("type", "any")
        t = "|".join(t) if isinstance(t, list) else t
        extra = []
        if "enum" in s:
            extra.append("one of " + ", ".join(f"`{v}`" for v in s["enum"]))
        for k, label in (("minimum", "min"), ("maximum", "max"), ("pattern", "pattern"), ("maxLength", "maxLength")):
            if k in s:
                extra.append(f"{label} `{s[k]}`")
        out.append(f"{indent}- `{name}` ({t}{', required' if name in req else ''})" + (f" - {'; '.join(extra)}" if extra else ""))
    return out


def markdown(spec: dict[str, Any]) -> str:
    comps = spec["components"]["schemas"]
    by_tag: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            by_tag.setdefault(op["tags"][0], []).append((method.upper(), path, op))
    lines = ["# GIS Stack API (v1)", "",
             "*Generated from the route table by `gisstack api docs` - do not edit. The machine-readable version is `docs/openapi.json` (also served at `/api/v1/openapi.json`).*", "",
             "## Conventions", "",
             "* Base path `/api/v1`. Every JSON answer is an envelope `{\"data\": ..., \"error\": null | {\"message\", \"code\", \"details\"?}, \"meta\": {\"version\": 1, \"request_id\"}}`.",
             "* Authentication: `Authorization: Bearer <token>` (an API token from `gisstack token create`, or the web access token), or the browser session cookie. "
             "Browser sessions must also send the header `X-GisStack: 1` on state-changing requests (CSRF protection); Bearer clients do not need it.",
             "* **Scopes**: `read` < `operate` < `admin`. A token's scope must be at least the route's scope, otherwise `403 forbidden`. API tokens only work on `/api/v1`.",
             "* Requests are validated against the schemas below; problems come back as `400 bad_request` with every problem listed in `error.details`. Unknown fields are rejected.",
             "* Long operations return `{\"data\": {\"job\": \"<id>\"}}`; follow them with `GET /jobs/{id}?since=<n>` (poll) and stop with `POST /jobs/{id}/cancel`. "
             "Only one mutating job runs at a time (`409 busy`).",
             "* Error codes are stable: `bad_request` 400, `config` 400, `plan` 400, `unauthorized` 401, `forbidden` 403, `not_found` 404, `feature_disabled` 404, `conflict` 409, "
             "`preflight` 409, `busy` 409, `too_large` 413, `rate_limited` 429, `transport` 502, `update` 502, `timeout` 504, `internal` 500.",
             "* The pre-1.0 `/api/...` routes still work for one more release and answer with `Deprecation: true` and a `Link: <...>; rel=\"successor-version\"` header. "
             "Each route below lists the old paths it replaces.", ""]
    for tag in sorted(by_tag):
        lines += [f"## {tag}", ""]
        for method, path, op in sorted(by_tag[tag], key=lambda x: (x[1], x[0])):
            lines += [f"### `{method} /api/v1{path}`", "", f"{op['summary']}.", "",
                      f"* Scope: **{op['x-required-scope']}**"]
            if op.get("x-replaces"):
                lines.append("* Replaces (deprecated): " + ", ".join(f"`{p}`" for p in op["x-replaces"]))
            if op.get("description"):
                lines.append(op["description"])
            for p in op.get("parameters", []):
                lines.append(f"* {p['in'].capitalize()} parameter `{p['name']}` ({p['schema'].get('type', 'string')}{', required' if p.get('required') else ''})"
                             + (f" - {p['description']}" if p.get("description") else ""))
            rb = op.get("requestBody")
            if rb:
                lines += ["* Request body (JSON):"] + _fields(rb["content"]["application/json"]["schema"], comps, "  ")
            ok = op["responses"]["200"]["content"]
            ct = next(iter(ok))
            if ct == "application/json":
                data = ok[ct]["schema"].get("properties", {}).get("data", {})
                if data.get("properties") and "job" not in data["properties"]:
                    lines += ["* Response `data`:"] + _fields(data, comps, "  ")
                elif data.get("properties", {}).get("job"):
                    lines.append("* Response `data`: `{\"job\": \"<id>\"}` (a background job)")
                elif data.get("type") == "array":
                    lines.append("* Response `data`: an array")
            else:
                lines.append(f"* Response: `{ct}` (no envelope)")
            codes = sorted(k for k in op["responses"] if k != "200")
            lines.append("* Errors: " + ", ".join(codes))
            lines.append("")
    lines += ["## Schemas", ""]
    for name in sorted(comps):
        lines += [f"### {name}", "", "```json", json.dumps(comps[name], indent=2, ensure_ascii=False), "```", ""]
    return "\n".join(lines).rstrip() + "\n"
