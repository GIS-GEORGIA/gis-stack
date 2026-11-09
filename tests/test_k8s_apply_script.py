"""apply.sh of the Kubernetes bundle: `replace_stale_pod` (regression for the StatefulSet "forced rollback" problem found on a real kind cluster:
after a failed upgrade the pod stayed on the broken revision although the template had been corrected, so the fix - and a rollback - never rolled out).

The function is run with a fake kubectl; the real behaviour (delete the pod, it comes back healthy on the right revision, the volume is kept) was
verified on kind and is covered by tests/integration/test_deploy_rollback_k8s.py."""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from gisstack import config, generate

KUBECTL = r'''#!/usr/bin/env bash
args="$*"
case "$args" in
  *"get statefulset postgis"*) echo -n "${REV}" ;;
  *"get pods -l app=postgis"*) echo "pod/postgis-0" ;;
  *"get pod/postgis-0"*Ready*) echo -n "${READY}" ;;
  *"get pod/postgis-0"*controller-revision-hash*) echo -n "${HASH}" ;;
  *delete*) echo "$args" >> "${LOG}" ;;
esac
'''


@unittest.skipUnless(shutil.which("bash"), "bash needed")
class ReplaceStalePodTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.td = Path(tempfile.mkdtemp())
        cfg = config.resolve({"project": "kt", "targets": ["k8s"], "deploy_target": "k8s", "components": {"postgis": True}}, persist_secrets=False, generate_secrets=True)
        generate.generate_bundle(cfg, cls.td / "b")
        cls.script = (cls.td / "b" / "k8s" / "apply.sh").read_text()
        cls.bin = cls.td / "bin"
        cls.bin.mkdir()
        for name, body in (("kubectl", KUBECTL), ("sleep", "#!/usr/bin/env bash\nexit 0\n")):
            p = cls.bin / name
            p.write_text(body)
            p.chmod(p.stat().st_mode | stat.S_IEXEC)
        start = cls.script.index("replace_stale_pod() {")
        cls.func = cls.script[start:cls.script.index("\n}\n", start) + 3]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def run_fn(self, rev, hash_, ready):
        log = self.td / "deleted.log"
        log.unlink(missing_ok=True)
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "REV": rev, "HASH": hash_, "READY": ready, "LOG": str(log)}
        r = subprocess.run(["bash", "-c", f'NS=ns\n{self.func}\nreplace_stale_pod postgis'], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout, (log.read_text() if log.exists() else "")

    def test_script_is_valid_bash_and_wired_into_apply_and_only(self):
        self.assertEqual(subprocess.run(["bash", "-n", "-c", self.script], capture_output=True).returncode, 0)
        self.assertGreaterEqual(self.script.count("replace_stale_pod postgis"), 2)            # apply and `only postgis`

    def test_not_ready_pod_on_an_outdated_revision_is_deleted(self):
        out, deleted = self.run_fn("postgis-new", "postgis-old", "False")
        self.assertIn("delete pod/postgis-0", deleted)
        self.assertIn("volume is kept", out)
        self.assertNotIn("pvc", deleted)                                                      # only the pod, never a volume

    def test_ready_pods_are_never_touched(self):
        self.assertEqual(self.run_fn("postgis-new", "postgis-old", "True")[1], "")           # a healthy pod rolls normally

    def test_not_ready_pod_on_the_current_revision_is_left_alone(self):
        self.assertEqual(self.run_fn("postgis-new", "postgis-new", "False")[1], "")          # still starting up: do not kill it

    def test_missing_revision_information_deletes_nothing(self):
        self.assertEqual(self.run_fn("", "postgis-old", "False")[1], "")


if __name__ == "__main__":
    unittest.main()
