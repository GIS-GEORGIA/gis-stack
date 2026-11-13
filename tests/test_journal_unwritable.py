"""A journal that cannot be written (disk full, read-only home) must not stop an operation - and must not hide its real error (audit 2026-11:
`journal.Operation.__exit__` raised OSError over the operation's own exception, and `__enter__` refused to start at all)."""
import errno
import os
import tempfile
import unittest
from unittest import mock

from gisstack import journal


class UnwritableJournal(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.home, True)
        env = mock.patch.dict(os.environ, {"GISSTACK_HOME": self.home})
        env.start()
        self.addCleanup(env.stop)
        real = os.open

        def full(path, flags, mode=0o777, *a, **k):
            if str(path).endswith(".jsonl"):
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(path, flags, mode, *a, **k)
        p = mock.patch("gisstack.journal.os.open", full)
        p.start()
        self.addCleanup(p.stop)

    def test_the_operation_runs_and_its_result_is_kept(self):
        ran = []
        with journal.Operation("deploy", project="p"):
            ran.append(1)
        self.assertEqual(ran, [1])

    def test_the_operations_own_error_is_not_replaced_by_the_journal_error(self):
        with self.assertRaises(ZeroDivisionError):
            with journal.Operation("deploy", project="p"):
                raise ZeroDivisionError("the operation's own error")

    def test_a_working_journal_still_records(self):
        mock.patch.stopall()
        env = mock.patch.dict(os.environ, {"GISSTACK_HOME": self.home})
        env.start()
        self.addCleanup(env.stop)
        with journal.Operation("deploy", project="p"):
            pass
        ops = journal.operations(5)
        self.assertEqual([o["status"] for o in ops], ["ok"])


if __name__ == "__main__":
    unittest.main()
