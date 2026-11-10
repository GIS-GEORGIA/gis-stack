"""The integration-test image builder (tests/integration/sysd.py): retries a flaky `docker build` and reports why it failed.  Regression for the
CI run where the ubuntu-22.04 image build failed once (apt mirror) and the log only said "returned non-zero exit status 1".  Unit tested, no docker."""
import subprocess
import unittest
from unittest import mock

from tests.integration import sysd


def done(rc, err=""):
    return subprocess.CompletedProcess(["docker"], rc, stdout="", stderr=err)


class BuildTests(unittest.TestCase):
    def test_a_transient_failure_is_retried_until_it_works(self):
        with mock.patch.object(sysd, "_docker", side_effect=[done(1, "E: Failed to fetch"), done(1, "Temporary failure resolving"), done(0)]) as d, \
                mock.patch.object(sysd.time, "sleep") as sleep:
            sysd.build("t:1", "/ctx", attempts=3, pause=20)
        self.assertEqual(d.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [20, 40])             # backing off, not hammering the mirror

    def test_success_builds_once_without_waiting(self):
        with mock.patch.object(sysd, "_docker", return_value=done(0)) as d, mock.patch.object(sysd.time, "sleep") as sleep:
            sysd.build("t:1", "/ctx")
        self.assertEqual(d.call_count, 1)
        sleep.assert_not_called()

    def test_persistent_failure_reports_the_real_reason(self):
        with mock.patch.object(sysd, "_docker", return_value=done(1, "E: Unable to locate package systemd")), mock.patch.object(sysd.time, "sleep"):
            with self.assertRaises(RuntimeError) as cm:
                sysd.build("t:1", "/ctx", attempts=2)
        self.assertIn("failed 2 times", str(cm.exception))
        self.assertIn("Unable to locate package systemd", str(cm.exception))

    def test_an_existing_image_is_not_rebuilt(self):
        with mock.patch.object(sysd, "_docker", return_value=done(0)) as d, mock.patch.object(sysd, "build") as b:
            self.assertEqual(sysd.image("ubuntu-24.04"), "gisstack-it-ubuntu-24.04:1")
        b.assert_not_called()
        self.assertEqual(d.call_count, 1)


if __name__ == "__main__":
    unittest.main()
