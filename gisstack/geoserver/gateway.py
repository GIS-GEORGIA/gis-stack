"""How requests reach GeoServer: always *from the machine GeoServer runs on* (127.0.0.1), through the same transport as every other operation.

`CurlGateway` runs curl there (locally or over SSH).  The admin password travels on curl's stdin (`-K -`), never in an argument list or the
process table, and never to the browser.  `Gateway` is the tiny interface (tests plug in a fake or a direct-HTTP implementation)."""
from __future__ import annotations

import base64
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Optional

from .. import transport
from ..core.errors import DeployError

MAX_BODY = 8 * 1024 * 1024


@dataclass
class Response:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()


class Gateway:
    def request(self, method: str, path: str, *, query: Optional[dict[str, str]] = None, body: Optional[bytes] = None,
                content_type: str = "", accept: str = "", timeout: int = 30) -> Response:
        raise NotImplementedError


def _q(s: str) -> str:
    """Quote a value for a curl config file."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t") + '"'


class CurlGateway(Gateway):
    def __init__(self, mode: str, ssh: Optional[dict[str, Any]], port: int, user: str, password: str) -> None:
        self.mode, self.ssh, self.port, self.user, self.password = mode, ssh, int(port), user, password

    def request(self, method: str, path: str, *, query: Optional[dict[str, str]] = None, body: Optional[bytes] = None,
                content_type: str = "", accept: str = "", timeout: int = 30) -> Response:
        if not path.startswith("/") or ".." in path or "?" in path or "#" in path or "\\" in path:
            raise DeployError("geoserver: bad path")
        url = f"http://127.0.0.1:{self.port}/geoserver{path}" + ("?" + urllib.parse.urlencode(query) if query else "")
        cfg = [f"url = {_q(url)}", f"request = {_q(method)}", f"user = {_q(self.user + ':' + self.password)}", f"max-time = {int(timeout)}",
               "silent", "show-error", 'header = "X-GisStack: 1"']
        if accept:
            cfg.append(f"header = {_q('Accept: ' + accept)}")
        if body is not None:
            cfg += [f"header = {_q('Content-Type: ' + (content_type or 'application/json'))}", f"data-raw = {_q(body.decode('utf-8'))}"]
        script = ("t=$(mktemp -d) || exit 1; trap 'rm -rf \"$t\"' EXIT; "
                  f"curl -K - --max-filesize {MAX_BODY} -D \"$t/h\" -o \"$t/b\" -w '%{{http_code}}'  >\"$t/s\" 2>\"$t/e\"; rc=$?; "
                  "echo \"RC $rc\"; echo \"STATUS $(cat \"$t/s\")\"; sed -n 's/\\r$//; s/^\\([A-Za-z-]*\\): \\(.*\\)$/HDR \\1: \\2/p' \"$t/h\"; "
                  "echo \"ERR $(head -c 300 \"$t/e\" | tr '\\n' ' ')\"; echo \"BODY $(base64 -w0 \"$t/b\" 2>/dev/null)\"")
        out = transport.run(self.mode, self.ssh, script, timeout=timeout + 15, input_text="\n".join(cfg) + "\n")
        return parse_output(out, self.password)


def parse_output(out: str, secret: str = "") -> Response:
    rc, status, hdrs, err, body = 0, 0, {}, "", b""
    for line in out.splitlines():
        if line.startswith("RC "):
            rc = int(line[3:] or 0)
        elif line.startswith("STATUS "):
            status = int(line[7:] or 0) if line[7:].strip().isdigit() else 0
        elif line.startswith("HDR "):
            k, _, v = line[4:].partition(": ")
            hdrs[k.lower()] = v
        elif line.startswith("ERR "):
            err = line[4:].strip()
        elif line.startswith("BODY "):
            try:
                body = base64.b64decode(line[5:], validate=False)
            except ValueError:
                body = b""
    if rc != 0 or status == 0:
        reason = {7: "connection refused", 28: "timed out", 63: "response too large", 22: "http error"}.get(rc, f"curl exit {rc}")
        msg = f"geoserver: {reason}" + (f" ({err})" if err and secret not in err else "")
        raise DeployError(msg)
    return Response(status, hdrs, body)


def local_port(cfg: dict[str, Any]) -> int:
    return int(cfg["tomcat"]["port"])


def for_config(cfg: dict[str, Any], mode: str, ssh: Optional[dict[str, Any]]) -> CurlGateway:
    if cfg.get("deploy_target") != "native":
        raise DeployError("GeoServer-ის მენეჯერი ჯერ მხოლოდ native სამიზნეზეა")
    gs = cfg["geoserver"]
    if not gs.get("admin_password"):
        raise DeployError("geoserver.admin_password ცარიელია")
    return CurlGateway(mode, ssh, local_port(cfg), gs["admin_user"], gs["admin_password"])


SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}$")
