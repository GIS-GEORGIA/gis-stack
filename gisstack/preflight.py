"""Pre-flight checks: can this machine take the selected stack? (audit R-04)

One read-only shell script collects facts (OS, architecture, memory, free disk, clock sync, reachability of the download
hosts, DNS of the public name) here or on a server; `evaluate()` is pure and turns them into structured results:
`{"id", "status": "ok" | "warn" | "fail", "message", "detail"}`.  Only "fail" blocks a deployment."""
from __future__ import annotations

import re
import shlex
from typing import Any, Optional

from . import generate, transport

OK, WARN, FAIL = "ok", "warn", "fail"
SUPPORTED_ARCH = ("x86_64", "aarch64", "arm64", "amd64")
# (os id, major version or None for "any") that the installer has been verified on
VERIFIED_NATIVE = {"ubuntu": ("22.04", "24.04"), "debian": ("12",), "rocky": ("9",), "almalinux": ("9",), "rhel": ("9",), "centos": ("9",)}

# hosts the installer downloads from, per component (native); containers pull from registries instead
HOSTS_NATIVE = {
    "base": ["archive.ubuntu.com"],
    "postgis": ["apt.postgresql.org", "download.postgresql.org"],
    "tomcat": ["dlcdn.apache.org", "archive.apache.org"],
    "geoserver": ["downloads.sourceforge.net"],
    "mapproxy": ["pypi.org", "files.pythonhosted.org"],
    "caddy": ["github.com"],
}
HOSTS_CONTAINERS = ["docker.osgeo.org", "registry-1.docker.io"]
_DOMAIN_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]*[A-Za-z0-9])?$")


def hosts_for(cfg: dict[str, Any], target: str) -> list[str]:
    if target != "native":
        return list(HOSTS_CONTAINERS)
    comps = generate.effective_components(cfg, "native")
    out: list[str] = []
    for key, hosts in HOSTS_NATIVE.items():
        if key == "base" or comps.get(key):
            out += hosts
    return out


def needs(cfg: dict[str, Any], target: str) -> dict[str, int]:
    """Rough requirements (MB) from the selected components."""
    comps = generate.effective_components(cfg, target)
    disk, mem = 2000, 1024
    if comps.get("postgis"):
        disk, mem = disk + 1000, mem + 512
    if comps.get("geoserver"):
        disk, mem = disk + 2000, mem + 1536
    if comps.get("qgis_server"):
        disk, mem = disk + 1500, mem + 512
    if comps.get("mapproxy"):
        disk, mem = disk + 500, mem + 256
    return {"disk_mb": disk, "mem_mb": mem}


def script(cfg: dict[str, Any], target: str) -> str:
    """Read-only fact collection; prints key=value lines."""
    lines = [
        "set +e",
        '. /etc/os-release 2>/dev/null; echo "os_id=${ID:-unknown}"; echo "os_version=${VERSION_ID:-}"',
        'echo "arch=$(uname -m)"',
        'echo "mem_mb=$(awk \'/MemTotal/ {print int($2/1024)}\' /proc/meminfo 2>/dev/null)"',
        'echo "cpus=$(nproc 2>/dev/null)"',
        'for p in / /var /opt; do echo "disk_$p=$(df -Pm "$p" 2>/dev/null | awk \'NR==2 {print $4}\')"; done',
        'echo "ntp=$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo unknown)"',
    ]
    for h in hosts_for(cfg, target):
        lines.append(f'curl -sS -o /dev/null -I -m 8 {shlex.quote("https://" + h)} >/dev/null 2>&1 && echo "reach_{h}=ok" || echo "reach_{h}=fail"')
    domain, tls = str(cfg.get("domain", "")), cfg.get("tls", "none")
    if tls != "none" and _DOMAIN_RE.match(domain) and domain != "localhost" and not re.fullmatch(r"[0-9.]+", domain):
        lines.append(f'echo "dns={shlex.quote(domain)}=$(getent hosts {shlex.quote(domain)} 2>/dev/null | awk \'{{print $1; exit}}\')"')
    return "\n".join(lines) + "\n"


def parse(text: str) -> dict[str, str]:
    facts: dict[str, str] = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep and re.fullmatch(r"[A-Za-z0-9_./\-]+", k):
            facts[k] = v.strip()
    return facts


def _int(v: Optional[str]) -> Optional[int]:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


def _c(cid: str, status: str, message: str, detail: str = "") -> dict[str, str]:
    return {"id": cid, "status": status, "message": message, "detail": detail}


def evaluate(cfg: dict[str, Any], target: str, facts: dict[str, str]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    need = needs(cfg, target)

    osid, ver, arch = facts.get("os_id", "unknown"), facts.get("os_version", ""), facts.get("arch", "")
    label = f"{osid} {ver}".strip()
    if target == "native":
        if osid in VERIFIED_NATIVE and (ver in VERIFIED_NATIVE[osid] or ver.split(".")[0] in VERIFIED_NATIVE[osid]):
            out.append(_c("os", OK, "ოპერაციული სისტემა მხარდაჭერილია", label))
        elif osid in VERIFIED_NATIVE or osid in ("fedora", "linuxmint", "pop", "raspbian"):
            out.append(_c("os", WARN, "ეს ვერსია გადამოწმებული არ არის — ინსტალაცია შეიძლება ვერ მოხერხდეს", label))
        else:
            out.append(_c("os", FAIL, "ოპერაციული სისტემა native ინსტალაციისთვის მხარდაჭერილი არ არის", label))
    else:
        out.append(_c("os", OK, "ოპერაციული სისტემა", label))

    if arch in SUPPORTED_ARCH:
        out.append(_c("arch", OK, "პროცესორის არქიტექტურა მხარდაჭერილია", arch))
    else:
        out.append(_c("arch", FAIL, "პროცესორის არქიტექტურა მხარდაჭერილი არ არის", arch or "?"))

    mem = _int(facts.get("mem_mb"))
    if mem is None:
        out.append(_c("memory", WARN, "მეხსიერების ზომა ვერ დადგინდა"))
    elif mem < need["mem_mb"] * 0.6:
        out.append(_c("memory", FAIL, "მეხსიერება ძალიან ცოტაა არჩეული პროგრამებისთვის", f"{mem} MB < {need['mem_mb']} MB"))
    elif mem < need["mem_mb"]:
        out.append(_c("memory", WARN, "მეხსიერება რეკომენდებულზე ნაკლებია", f"{mem} MB < {need['mem_mb']} MB"))
    else:
        out.append(_c("memory", OK, "მეხსიერება საკმარისია", f"{mem} MB"))

    free = [v for v in (_int(facts.get("disk_/")), _int(facts.get("disk_/var")), _int(facts.get("disk_/opt"))) if v is not None]
    if not free:
        out.append(_c("disk", WARN, "თავისუფალი ადგილი ვერ დადგინდა"))
    else:
        least = min(free)
        if least < need["disk_mb"] * 0.6:
            out.append(_c("disk", FAIL, "დისკზე თავისუფალი ადგილი არ ჰყოფნის", f"{least} MB < {need['disk_mb']} MB"))
        elif least < need["disk_mb"]:
            out.append(_c("disk", WARN, "დისკზე თავისუფალი ადგილი რეკომენდებულზე ნაკლებია", f"{least} MB < {need['disk_mb']} MB"))
        else:
            out.append(_c("disk", OK, "დისკზე ადგილი საკმარისია", f"{least} MB"))

    ntp = facts.get("ntp", "unknown")
    if ntp == "yes":
        out.append(_c("time", OK, "საათი სინქრონიზებულია"))
    elif ntp == "no":
        out.append(_c("time", WARN, "საათი სინქრონიზებული არ არის (TLS სერტიფიკატები და ჩამოტვირთვები შეიძლება ჩავარდეს)"))
    else:
        out.append(_c("time", WARN, "საათის სინქრონიზაცია ვერ დადგინდა"))

    bad = sorted(k[len("reach_"):] for k, v in facts.items() if k.startswith("reach_") and v != "ok")
    if bad:
        out.append(_c("network", FAIL, "საჭირო ჩამოტვირთვის მისამართები მიუწვდომელია", ", ".join(bad)))
    elif any(k.startswith("reach_") for k in facts):
        out.append(_c("network", OK, "ჩამოტვირთვის მისამართები ხელმისაწვდომია"))
    else:
        out.append(_c("network", WARN, "ქსელის შემოწმება ვერ შესრულდა"))

    for k, v in facts.items():
        if k.startswith("dns"):
            _name, _, ip = v.partition("=")
            if ip:
                out.append(_c("dns", OK, "დომენის სახელი ითარგმნება IP-ად", f"{cfg.get('domain')} -> {ip}"))
            else:
                out.append(_c("dns", WARN, "დომენის სახელი ჯერ არ ითარგმნება IP-ად (HTTPS სერტიფიკატი ვერ გაიცემა)", str(cfg.get("domain"))))
    return out


def blocking(checks: list[dict[str, str]]) -> list[dict[str, str]]:
    return [c for c in checks if c["status"] == FAIL]


def run(cfg: dict[str, Any], mode: str, ssh: Optional[dict[str, Any]], target: Optional[str] = None) -> dict[str, Any]:
    """Collect facts here / on the server and judge them.  Raises DeployError when the machine cannot be reached."""
    t = target or cfg["deploy_target"]
    facts = parse(transport.run(mode, ssh, script(cfg, t), timeout=90))
    checks = evaluate(cfg, t, facts) if facts else [_c("preflight", WARN, "წინასწარმა შემოწმებამ პასუხი არ დააბრუნა")]
    return {"target": t, "checks": checks, "ok": not blocking(checks), "facts": facts}


def format_table(res: dict[str, Any]) -> str:
    icon = {OK: "✔", WARN: "!", FAIL: "✘"}
    rows = [f" {icon[c['status']]} {c['message']}" + (f"  [{c['detail']}]" if c["detail"] else "") for c in res["checks"]]
    return "\n".join(rows)
