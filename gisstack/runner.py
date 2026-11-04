"""The one place that starts external commands (audit R-01, A-01).

Every long-running or remote command goes through here so that it is

* **bounded**: an overall deadline and an idle timeout (no output for too long), both overridable by environment variables;
* **cancellable**: a `threading.Event` ends the command and its children;
* **fully killed**: the command runs in its own process group (POSIX) and the whole group gets SIGTERM, then SIGKILL after a
  short grace period, so a hung `ssh` or an installer's child processes never outlive the operation;
* **free of secrets in its output**: known secret values are replaced by a mask in every line that is yielded or logged.
"""
from __future__ import annotations

import contextlib
import os
import queue
import shlex
import signal
import subprocess
import threading
import time
from typing import Any, Callable, Iterable, Iterator, Optional, Sequence, Type

MASK = "••••••••"
GRACE_SECONDS = 5.0
DEADLINE_ENV, IDLE_ENV = "GISSTACK_DEADLINE", "GISSTACK_IDLE_TIMEOUT"
DEFAULT_DEADLINE, DEFAULT_IDLE = 3600.0, 900.0


class RunnerError(RuntimeError):
    pass


class Timeout(RunnerError):
    """The command exceeded its deadline or produced no output for too long."""


def _env_seconds(name: str, default: Optional[float]) -> Optional[float]:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    return None if v <= 0 else v          # 0 = no limit


def default_deadline() -> Optional[float]:
    return _env_seconds(DEADLINE_ENV, DEFAULT_DEADLINE)


def default_idle() -> Optional[float]:
    return _env_seconds(IDLE_ENV, DEFAULT_IDLE)


def redactor(secrets: Iterable[Any]) -> Callable[[str], str]:
    """Function that masks every given secret (longest first, so a secret that contains another is fully hidden)."""
    values = sorted({str(s) for s in secrets if s and len(str(s)) >= 4}, key=len, reverse=True)

    def mask(text: str) -> str:
        for v in values:
            if v in text:
                text = text.replace(v, MASK)
        return text
    return mask


def secrets_of(cfg: dict) -> list[str]:
    """Every value in a resolved config whose key looks like a password / token / secret."""
    found: list[str] = []

    def walk(node: Any, key: str = "") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, str(k))
        elif isinstance(node, str) and node and any(w in key.lower() for w in ("password", "token", "secret")):
            found.append(node)
    walk(cfg)
    return found


def _popen(cmd: Sequence[str], **kw: Any) -> subprocess.Popen[str]:
    if os.name == "posix":
        kw["start_new_session"] = True            # own process group: the whole tree can be signalled
    return subprocess.Popen(list(cmd), **kw)  # noqa: S603


def kill_tree(proc: subprocess.Popen[Any], grace: float = GRACE_SECONDS) -> None:
    """SIGTERM the process group, SIGKILL whatever is left after `grace` seconds, and reap the child."""
    if proc.poll() is not None and os.name != "posix":
        return

    def send(sig: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            if os.name == "posix":
                os.killpg(proc.pid, sig)
            elif sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()
    send(signal.SIGTERM)
    end = time.monotonic() + grace
    while proc.poll() is None and time.monotonic() < end:
        time.sleep(0.05)
    if os.name == "posix":
        send(signal.SIGKILL)               # children may have ignored TERM even though the leader exited
    elif proc.poll() is None:
        send(getattr(signal, "SIGKILL", signal.SIGTERM))
    with contextlib.suppress(Exception):
        proc.wait(timeout=grace)


def stream(cmd: Sequence[str], *, cwd: Optional[str] = None, env: Optional[dict] = None, stdin: Any = subprocess.DEVNULL,
           deadline: Optional[float] = None, idle: Optional[float] = None, stop: Optional[threading.Event] = None,
           redact: Optional[Callable[[str], str]] = None, error: Type[Exception] = RunnerError, echo: bool = True,
           grace: float = GRACE_SECONDS) -> Iterator[str]:
    """Run `cmd`, yielding its merged stdout/stderr line by line.

    Raises `error` when the command is missing, fails (non-zero exit) or times out.  If `stop` is set the command is
    ended and the generator returns normally (a user cancelling is not a failure).  deadline/idle in seconds; None = no limit."""
    mask = redact or (lambda s: s)
    if echo:
        yield mask("$ " + " ".join(shlex.quote(c) for c in cmd))
    try:
        proc = _popen(cmd, cwd=cwd, env=env, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                      bufsize=1, errors="replace")
    except FileNotFoundError as e:
        raise error(f"command not found: {cmd[0]}") from e
    assert proc.stdout is not None
    lines: queue.Queue[Optional[str]] = queue.Queue()

    def pump() -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                lines.put(line.rstrip("\n"))
        except (ValueError, OSError):
            pass
        finally:
            lines.put(None)
    threading.Thread(target=pump, daemon=True).start()

    started = last = time.monotonic()
    cancelled, reason = False, ""
    try:
        while True:
            now = time.monotonic()
            if stop is not None and stop.is_set():
                cancelled = True
                break
            if deadline is not None and now - started > deadline:
                reason = f"deadline of {int(deadline)} s exceeded"
                break
            if idle is not None and now - last > idle:
                reason = f"no output for {int(idle)} s"
                break
            try:
                item = lines.get(timeout=0.25)
            except queue.Empty:
                continue
            if item is None:
                break
            last = time.monotonic()
            yield mask(item)
    finally:                                   # also when the consumer stops early: no leaked pipe, process or child
        if proc.poll() is None or cancelled or reason:
            kill_tree(proc, grace)
        with contextlib.suppress(Exception):
            proc.stdout.close()
        proc.wait()
    if reason:
        raise (Timeout if error is RunnerError else error)(f"{reason}: {cmd[0]}")
    if cancelled:
        return
    if proc.returncode != 0:
        raise error(f"command failed (exit {proc.returncode}): {cmd[0]}")


def run(cmd: Sequence[str], *, timeout: float, stdin: Any = subprocess.DEVNULL, cwd: Optional[str] = None, env: Optional[dict] = None,
        error: Type[Exception] = RunnerError, grace: float = GRACE_SECONDS) -> subprocess.CompletedProcess[str]:
    """subprocess.run replacement: on timeout the whole process group is killed (a hung ssh does not linger)."""
    try:
        proc = _popen(cmd, cwd=cwd, env=env, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
    except FileNotFoundError as e:
        raise error(f"command not found: {e.filename}") from e
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as e:
        kill_tree(proc, grace)
        with contextlib.suppress(Exception):
            proc.communicate(timeout=grace)
        raise error("დრო ამოიწურა (სერვერი არ პასუხობს)") from e
    except BaseException:
        kill_tree(proc, grace)
        raise
    return subprocess.CompletedProcess(list(cmd), proc.returncode, out, err)
