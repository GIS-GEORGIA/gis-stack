"""Container-image availability for the integration tests - and for CI, as a command - with a failure that says WHICH kind of failure it is.

    python -m tests.integration.images [--engine docker|podman] [--expect-digest sha256:...] IMAGE...

Exit codes (so a workflow can tell an outage from a test failure):
  0   every image is usable (present locally, or pulled now)
  10  REGISTRY UNAVAILABLE: the registry itself does not answer (timeout, connection refused, HTTP 5xx on /v2/)
  11  PULL FAILED: the registry answers but the image could not be pulled (not found, denied, broken manifest)
  12  DIGEST MISMATCH: the image is present but its digest is not the one that was expected

Behaviour for tests: `require()` skips when GISSTACK_REQUIRE_IMAGES is not set (a developer without network), and FAILS with a message that starts with
the classification when it is set (CI): an outage must never end as a green run that quietly skipped the tests that needed the image.  Pulls are
retried a bounded number of times with back-off (transient errors) - never forever, and the last error is always reported."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from typing import Optional

OK, REGISTRY_UNAVAILABLE, PULL_FAILED, DIGEST_MISMATCH = 0, 10, 11, 12
LABEL = {OK: "ok", REGISTRY_UNAVAILABLE: "REGISTRY UNAVAILABLE", PULL_FAILED: "IMAGE PULL FAILED", DIGEST_MISMATCH: "IMAGE DIGEST MISMATCH"}
ATTEMPTS = int(os.environ.get("GISSTACK_IMAGE_PULL_ATTEMPTS", "3"))
BACKOFF = float(os.environ.get("GISSTACK_IMAGE_PULL_BACKOFF", "5"))


def geoserver_image() -> str:
    """The GeoServer image a default stack deploys (config's default channel -> catalog).  The tests must guard THIS image: a hand-written tag drifted from it."""
    from gisstack import catalog, config
    return str(catalog.GEOSERVER_CHANNELS[str(config.default_config()["geoserver"]["channel"])]["image"])


def registry_of(image: str) -> str:
    first = image.split("/")[0]
    return first if ("." in first or ":" in first or first == "localhost") and "/" in image else "registry-1.docker.io"


def registry_status(registry: str, timeout: float = 15.0, scheme: str = "https") -> tuple[bool, str]:
    """Is the registry's API alive?  Any HTTP answer below 500 (200, or 401 asking for a token) counts as alive; 5xx, timeouts and refused connections do not."""
    try:
        with urllib.request.urlopen(f"{scheme}://{registry}/v2/", timeout=timeout) as r:   # noqa: S310 - fixed https endpoint
            return r.status < 500, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return e.code < 500, f"HTTP {e.code}"
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return False, str(getattr(e, "reason", e))[:160]


def _run(engine: str, *args: str, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run([engine, *args], capture_output=True, text=True, timeout=timeout)


def local_digest(engine: str, image: str) -> Optional[str]:
    """The registry digest(s) the local copy is known by, as one string (empty when the image is not present)."""
    fmt = "{{range .RepoDigests}}{{.}} {{end}}"
    r = _run(engine, "image", "inspect", "--format", fmt, image, timeout=60)
    return r.stdout.strip() if r.returncode == 0 else None


def ensure(engine: str, image: str, expect_digest: str = "", scheme: str = "https", sleep=time.sleep) -> tuple[int, str]:
    """(exit code, message).  Present locally -> ok without touching the network; otherwise pull with bounded retries."""
    have = local_digest(engine, image)
    last = ""
    if have is None:
        for attempt in range(1, ATTEMPTS + 1):
            r = _run(engine, "pull", "-q", image)
            if r.returncode == 0:
                have = local_digest(engine, image) or ""
                break
            last = (r.stderr or r.stdout).strip().splitlines()[-1][:300] if (r.stderr or r.stdout).strip() else f"exit {r.returncode}"
            if attempt < ATTEMPTS:
                sleep(BACKOFF * attempt)
        else:
            up, detail = registry_status(registry_of(image), scheme=scheme)
            if not up:
                return REGISTRY_UNAVAILABLE, f"{registry_of(image)} does not answer ({detail}); pull of {image} failed after {ATTEMPTS} attempts: {last}"
            return PULL_FAILED, f"{registry_of(image)} answers ({detail}) but pulling {image} failed after {ATTEMPTS} attempts: {last}"
    if expect_digest and expect_digest not in (have or ""):
        return DIGEST_MISMATCH, f"{image} is present as [{have}] but sha256 {expect_digest} was expected"
    return OK, f"{image} {have or ''}".strip()


def require(engine: str, *images: str, expect_digest: str = "") -> None:
    """For setUpClass: raise unittest.SkipTest (no network, developer machine) or AssertionError (GISSTACK_REQUIRE_IMAGES=1, CI) when an image is unusable."""
    if not shutil.which(engine):
        raise unittest.SkipTest(f"{engine} not installed")
    for image in images:
        code, msg = ensure(engine, image, expect_digest)
        if code != OK:
            text = f"{LABEL[code]}: {msg}"
            if os.environ.get("GISSTACK_REQUIRE_IMAGES") == "1":
                raise AssertionError(text + "  (this is an infrastructure failure, not a test assertion; the tests that need this image did not run)")
            raise unittest.SkipTest(text)


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--engine", default="docker", choices=("docker", "podman"))
    ap.add_argument("--expect-digest", default="")
    ap.add_argument("images", nargs="+")
    a = ap.parse_args(argv)
    worst = OK
    for image in a.images:
        code, msg = ensure(a.engine, image, a.expect_digest)
        print(f"{LABEL[code]}: {msg}")
        if code != OK:
            print(f"::error title={LABEL[code]}::{msg}", file=sys.stderr)
            worst = worst or code
    return worst


if __name__ == "__main__":
    sys.exit(main())
