"""Background jobs shared by the legacy /api routes and /api/v1: one registry, one lock (one mutating operation at a time)."""
from __future__ import annotations

import shutil
import threading
import time
import uuid
from typing import Any, Callable, Iterator, Optional

from .. import generate
from ..core import paths
from ..core.errors import GisStackError
from .registry import ApiError

MAX_JOB_LINES = 50_000
MAX_JOBS = 50
JOB_TTL = 3600


class Job:
    def __init__(self) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.lines: list[str] = []
        self.done = False
        self.ok = False
        self.started = time.time()
        self.lock = threading.Lock()
        self.stop = threading.Event()      # set by /cancel (log following)

    def add(self, line: str) -> None:
        with self.lock:
            if len(self.lines) < MAX_JOB_LINES:
                self.lines.append(line)
            elif len(self.lines) == MAX_JOB_LINES:
                self.lines.append("… (log truncated)")


def register(app: Any, job: Job) -> None:
    """Keep the job registry bounded: drop finished jobs after an hour and the oldest ones beyond MAX_JOBS."""
    jobs: dict[str, Job] = app.config["JOBS"]
    now = time.time()
    for jid in [k for k, j in jobs.items() if j.done and now - j.started > JOB_TTL]:
        del jobs[jid]
    while len(jobs) >= MAX_JOBS:
        oldest = next((k for k, j in jobs.items() if j.done), None)
        if oldest is None:
            break
        del jobs[oldest]
    jobs[job.id] = job


def start(app: Any, steps: Callable[[Job, Any], Iterator[str]], *, locked: bool = True, cfg: Optional[dict[str, Any]] = None,
          make_bundle: bool = False, cleanup_bundle: bool = False) -> Job:
    """Run `steps(job, bundle_dir)` in a daemon thread.  `locked` serialises it with every other mutating job (409 `busy` otherwise);
    `make_bundle` renders the bundle for `cfg` first; `cleanup_bundle` removes the local bundle afterwards (it holds secrets)."""
    lock: threading.Lock = app.config["DEPLOY_LOCK"]
    if locked and not lock.acquire(blocking=False):
        raise ApiError(409, "busy", "another operation is already running")
    job = Job()
    register(app, job)
    bundle_dir = (paths.bundles_dir() / f"{cfg['project']}-{job.id}") if (cfg and make_bundle) else None

    def run() -> None:
        try:
            if bundle_dir is not None and cfg is not None:
                job.add("== bundle generation")
                generate.generate_bundle(cfg, bundle_dir)
                bundle_dir.chmod(0o700)
            for line in steps(job, bundle_dir):
                job.add(line)
            job.ok = True
        except GisStackError as e:
            job.add(f"ERROR: {e}")
        except Exception as e:  # noqa: BLE001 - surface anything in the job log
            job.add(f"ERROR: {type(e).__name__}: {e}")
        finally:
            if cleanup_bundle and bundle_dir is not None:
                shutil.rmtree(bundle_dir, ignore_errors=True)
            job.done = True
            if locked:
                lock.release()
    threading.Thread(target=run, daemon=True).start()
    return job
