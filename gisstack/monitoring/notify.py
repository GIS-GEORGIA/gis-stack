"""Alert notifications (Phase 8): webhook and e-mail providers.  A failing provider never breaks monitoring: the failure becomes a
warning event.  Secrets (SMTP password) come from the environment / a secret file, never from a config file; the full webhook URL
(which may contain a token) is never logged - only its host."""
from __future__ import annotations

import json
import smtplib
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable, Optional

from ..core import log
from ..core.errors import GisStackError
from .. import plugins, secretsrc

_log = log.get("notify")
LOOPBACK = ("localhost", "127.0.0.1", "::1", "[::1]")


class NotifyError(GisStackError):
    code = "notify"


def _loopback(host: str) -> bool:
    return (host or "").lower() in LOOPBACK


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None                                    # a webhook that redirects is misconfigured; never follow it somewhere else


def text_of(n: dict[str, Any]) -> str:
    mark = {"firing": "🔴" if n["level"] == "crit" else "🟠", "resolved": "🟢"}.get(n["state"], "")
    where = " @ ".join(x for x in (n.get("project"), n.get("host")) if x)
    return f"{mark} [{n['level'].upper()}] {n['message']}" + (f"  ({where})" if where else "")


class WebhookProvider:
    """POST JSON {"text": ..., "alert": {...}} (Slack / Mattermost / Rocket.Chat / generic receivers)."""

    def __init__(self, url: str, allow_insecure: bool = False, timeout: float = 10.0, retries: int = 2, sleep: Callable[[float], None] = time.sleep) -> None:
        u = urllib.parse.urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise NotifyError("webhook: only http(s) URLs are allowed")
        if u.scheme == "http" and not (_loopback(u.hostname) or allow_insecure):
            raise NotifyError("webhook: https is required (http only for localhost, or set allow_insecure_webhooks)")
        self.url, self.host, self.timeout, self.retries, self.sleep = url, u.hostname, timeout, retries, sleep
        self.name = f"webhook:{u.hostname}"

    def send(self, n: dict[str, Any]) -> None:
        body = json.dumps({"text": text_of(n), "alert": n}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url,  # noqa: S310 - scheme checked in __init__
                                      data=body, method="POST", headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "gisstack-monitor"})
        opener = urllib.request.build_opener(_NoRedirect)
        last: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                with opener.open(req, timeout=self.timeout) as r:                   # noqa: S310 - scheme checked in __init__
                    if 200 <= r.status < 300:
                        return
                    last = NotifyError(f"HTTP {r.status}")
            except urllib.error.HTTPError as e:
                last = NotifyError(f"HTTP {e.code}")
                if 400 <= e.code < 500 and e.code != 429:
                    break                                                           # retrying a rejected request does not help
            except (urllib.error.URLError, OSError) as e:
                last = NotifyError(str(getattr(e, "reason", e)))
            if attempt < self.retries:
                self.sleep(0.5 * (2 ** attempt))
        raise NotifyError(f"{self.name}: {last}")


class EmailProvider:
    def __init__(self, host: str, port: int, sender: str, to: list[str], user: str = "", starttls: Optional[bool] = None, timeout: float = 15.0,
                 password: Optional[str] = None) -> None:
        for a in [sender, *to]:
            if not a or any(c in a for c in "\r\n,<>") or "@" not in a:
                raise NotifyError(f"email: bad address {a!r}")
        if not to:
            raise NotifyError("email: no recipients")
        self.host, self.port, self.sender, self.to, self.user = host, int(port), sender, list(to), user
        self.starttls = (not _loopback(host)) if starttls is None else bool(starttls)
        self.timeout, self._password = timeout, password
        self.name = f"email:{host}"

    def password(self) -> Optional[str]:
        return self._password or secretsrc.EnvProvider().get("smtp.password") or secretsrc.FileProvider().get("smtp.password")

    def send(self, n: dict[str, Any]) -> None:
        msg = EmailMessage()
        msg["Subject"] = " ".join(f"[gisstack] {n['level'].upper()} {n['state']}: {n['message']}".split())[:200]       # one line, no header injection
        msg["From"], msg["To"] = self.sender, ", ".join(self.to)
        msg.set_content(text_of(n) + "\n\n" + json.dumps({k: v for k, v in n.items() if k != "message"}, indent=2, ensure_ascii=False, default=str))
        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as s:
                if self.starttls:
                    s.starttls(context=ssl.create_default_context())
                pw = self.password()
                if self.user and pw:
                    s.login(self.user, pw)
                s.send_message(msg)
        except (smtplib.SMTPException, OSError) as e:
            raise NotifyError(f"{self.name}: {type(e).__name__}") from e                  # no server text: it may echo credentials


def load_providers(path: Optional[Path]) -> list[Any]:
    """Providers from ~/.gisstack/monitor/notify.json: {"webhooks": [{"url": ...}], "email": {host, port, from, to, user, starttls}}."""
    if path is None or not Path(path).is_file():
        return []
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise NotifyError(f"{path}: {e}") from e
    if not isinstance(data, dict) or set(data) - {"webhooks", "email", "allow_insecure_webhooks"}:
        raise NotifyError(f"{path}: allowed keys are webhooks, email, allow_insecure_webhooks")
    out: list[Any] = []
    insecure = data.get("allow_insecure_webhooks") is True
    for w in data.get("webhooks", []):
        if not isinstance(w, dict) or not isinstance(w.get("url"), str):
            raise NotifyError(f"{path}: every webhook needs a url")
        out.append(WebhookProvider(w["url"], allow_insecure=insecure))
    mail = data.get("email")
    if mail is not None:
        if not isinstance(mail, dict) or not {"host", "from", "to"} <= set(mail) or set(mail) - {"host", "port", "from", "to", "user", "starttls"}:
            raise NotifyError(f"{path}: email needs host, from, to (optional: port, user, starttls)")
        if "password" in mail:
            raise NotifyError(f"{path}: the SMTP password does not belong in a config file - use GISSTACK_SMTP_PASSWORD or GISSTACK_SMTP_PASSWORD_FILE")
        out.append(EmailProvider(mail["host"], int(mail.get("port", 587)), mail["from"], [mail["to"]] if isinstance(mail["to"], str) else list(mail["to"]), mail.get("user", ""),
                                 mail.get("starttls")))
    return out


def plugin_notifiers() -> list[Any]:
    """Notification channels contributed by MonitoringProvider plugins (none unless the `plugins` feature is on)."""
    out: list[Any] = []
    for plug in plugins.get("monitoring_provider"):
        out += plugins.safe_call(plug, "notifiers", default=[]) or []
    return out


def dispatch(notes: list[dict[str, Any]], providers: list[Any], on_failure: Optional[Callable[[str, str], None]] = None) -> list[dict[str, Any]]:
    """Send every notification through every provider.  Returns [{provider, rule, subject, ok, error}]; never raises."""
    results = []
    for n in notes:
        for p in providers:
            try:
                p.send(n)
                results.append({"provider": p.name, "rule": n["rule"], "subject": n["subject"], "ok": True, "error": ""})
            except Exception as e:  # noqa: BLE001 - a broken receiver must not stop monitoring
                msg = str(e)
                results.append({"provider": p.name, "rule": n["rule"], "subject": n["subject"], "ok": False, "error": msg})
                _log.warning("notification failed: %s", msg)
                if on_failure:
                    on_failure(p.name, msg)
    return results
