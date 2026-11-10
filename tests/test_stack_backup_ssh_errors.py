"""--mode ssh: a refused host key must reach the operator as a host-key error, never as "the workload is not running" (found by the real sshd test)."""
import subprocess
import unittest
from unittest import mock

from gisstack import config
from gisstack.backups import provider
from gisstack.core.errors import HostKeyChanged, HostKeyUnknown

CHANGED = b"@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\nHost key for [127.0.0.1]:2222 has changed and you have requested strict checking.\n"
UNKNOWN = b"No ED25519 host key is known for [127.0.0.1]:2222 and you have requested strict checking.\nHost key verification failed.\n"


def prov():
    cfg = config.resolve({"project": "sshe", "targets": ["compose"], "deploy_target": "compose"}, generate_secrets=False, persist_secrets=False)
    return provider.ComposeBackupProvider(cfg, "ssh", {"host": "127.0.0.1", "port": 2222, "user": "root", "key": "/nonexistent"})


def done(rc, err):
    return subprocess.CompletedProcess([], rc, b"", err)


class SshErrorTests(unittest.TestCase):
    def test_changed_and_unknown_host_keys_are_raised_even_from_the_running_check(self):
        with mock.patch.object(provider.transport, "argv", lambda *a, **k: ["ssh"]):
            with mock.patch.object(provider.subprocess, "run", return_value=done(255, CHANGED)), self.assertRaises(HostKeyChanged):
                prov().running("postgres")
            with mock.patch.object(provider.subprocess, "run", return_value=done(255, UNKNOWN)), self.assertRaises(HostKeyUnknown):
                prov().running("postgres")
            with mock.patch.object(provider.subprocess, "run", return_value=subprocess.CompletedProcess([], 255, "", UNKNOWN.decode())), self.assertRaises(HostKeyUnknown):
                prov().host("postgres", "true")                                                  # host() runs in text mode

    def test_other_failures_keep_their_meaning(self):
        with mock.patch.object(provider.transport, "argv", lambda *a, **k: ["ssh"]):
            with mock.patch.object(provider.subprocess, "run", return_value=done(1, b"container is not running")):
                self.assertFalse(prov().running("postgres"))                                    # the workload really is down
            with mock.patch.object(provider.subprocess, "run", return_value=done(255, b"Connection refused")):
                self.assertFalse(prov().running("postgres"))                                    # unreachable: not a trust problem, keeps the old behaviour


if __name__ == "__main__":
    unittest.main()
