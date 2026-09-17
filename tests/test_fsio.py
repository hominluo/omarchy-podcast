import os
import stat
import tempfile
import unittest
from unittest import mock

from engine import fsio


class FsioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="podcast-fsio-")
        self.dir = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def test_atomic_write_lands_privately_with_no_temp_left(self):
        target = os.path.join(self.dir, "nested", "daemon.json")
        fsio.atomic_write(target, '{"pid": 1}')
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), '{"pid": 1}')
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        self.assertEqual(os.listdir(os.path.dirname(target)), ["daemon.json"])

    def test_atomic_write_honours_mode_and_bytes(self):
        target = os.path.join(self.dir, "subscriptions.opml")
        fsio.atomic_write(target, b"<opml/>", mode=0o644)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o644)
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), b"<opml/>")

    def test_symlinked_destination_is_replaced_not_followed(self):
        victim = os.path.join(self.dir, "victim")
        with open(victim, "w", encoding="utf-8") as handle:
            handle.write("keep me")
        target = os.path.join(self.dir, "credentials.json")
        os.symlink(victim, target)
        fsio.atomic_write(target, "secret")
        self.assertFalse(os.path.islink(target))
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "secret")
        with open(victim, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "keep me")

    def test_temp_name_collision_is_retried_and_the_other_file_kept(self):
        target = os.path.join(self.dir, "state.json")
        planted = os.path.join(self.dir, ".state.json.aaaa.tmp")
        with open(planted, "w", encoding="utf-8") as handle:
            handle.write("theirs")
        with mock.patch.object(fsio.secrets, "token_hex", side_effect=["aaaa", "bbbb"]):
            fsio.atomic_write(target, "ours")
        with open(planted, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "theirs")
        self.assertEqual(sorted(os.listdir(self.dir)), [".state.json.aaaa.tmp", "state.json"])

    def test_open_new_refuses_to_reuse_a_planted_symlink(self):
        victim = os.path.join(self.dir, "victim")
        with open(victim, "w", encoding="utf-8") as handle:
            handle.write("keep me")
        os.symlink(victim, os.path.join(self.dir, ".dl.aaaa"))
        with mock.patch.object(fsio.secrets, "token_hex", side_effect=["aaaa", "bbbb"]):
            fd, path = fsio.open_new(self.dir, ".dl.")
        os.close(fd)
        self.assertEqual(os.path.basename(path), ".dl.bbbb")
        with open(victim, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "keep me")

    def test_open_nofollow_refuses_a_symlinked_partial(self):
        victim = os.path.join(self.dir, "victim")
        with open(victim, "w", encoding="utf-8") as handle:
            handle.write("keep me")
        part = os.path.join(self.dir, "episode.mp3.abcdef.part")
        os.symlink(victim, part)
        with self.assertRaises(OSError):
            fsio.open_nofollow(part, os.O_WRONLY | os.O_APPEND)
        with self.assertRaises(OSError):
            fsio.open_nofollow(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with open(victim, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "keep me")

    def test_open_nofollow_appends_to_our_own_partial(self):
        part = os.path.join(self.dir, "episode.mp3.abcdef.part")
        fd = fsio.open_nofollow(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        os.write(fd, b"abc")
        os.close(fd)
        fd = fsio.open_nofollow(part, os.O_WRONLY | os.O_APPEND)
        os.write(fd, b"def")
        os.close(fd)
        with open(part, "rb") as handle:
            self.assertEqual(handle.read(), b"abcdef")
        self.assertEqual(stat.S_IMODE(os.stat(part).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
