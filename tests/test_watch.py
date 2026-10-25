import unittest
from unittest import mock

from office import watch
from office.watch import NOTIFY_GAP, Watcher


def summ(*ids):
    return {"count": len(ids), "waiting": [{"id": i, "name": f"n{i}", "text": f"t{i}?"} for i in ids]}


class TestWatcher(unittest.TestCase):
    def test_existing_waits_are_not_notified(self):
        w = Watcher(["a"])
        self.assertIsNone(w.tick(summ("a"), 1000))

    def test_new_wait_notifies_once(self):
        w = Watcher([])
        title, msg, sid = w.tick(summ("a"), 1000)
        self.assertEqual((title, sid), ("기다림 1", "a"))
        self.assertIn("na: ta?", msg)
        self.assertIsNone(w.tick(summ("a"), 1000 + NOTIFY_GAP + 1))

    def test_bursts_are_grouped_after_gap(self):
        w = Watcher([])
        w.tick(summ("a"), 1000)
        self.assertIsNone(w.tick(summ("a", "b"), 1010))
        self.assertIsNone(w.tick(summ("a", "b", "c"), 1020))
        title, msg, sid = w.tick(summ("a", "b", "c"), 1000 + NOTIFY_GAP)
        self.assertEqual((title, sid), ("기다림 3", "b"))
        self.assertIn("nb 외 1명", msg)

    def test_answered_before_notify_is_dropped(self):
        w = Watcher([])
        w.tick(summ("a"), 1000)
        w.tick(summ("a", "b"), 1010)
        self.assertIsNone(w.tick(summ("a"), 1000 + NOTIFY_GAP))

    def test_wait_again_after_answer_is_new(self):
        w = Watcher(["a"])
        w.tick(summ(), 1000)
        self.assertIsNotNone(w.tick(summ("a"), 2000))

    def test_english_title(self):
        title, msg, _ = Watcher([], lang="en").tick(summ("a", "b"), 1000)
        self.assertEqual(title, "Waiting: 2")
        self.assertIn("na +1 more", msg)


class TestNotify(unittest.TestCase):
    @unittest.skipIf(watch.os.name == "nt", "윈도우는 알림을 건너뛴다")
    def test_osascript_keeps_korean(self):
        """terminal-notifier 가 없을 때 — AppleScript 는 \\uXXXX 를 모른다(그대로 넣으면 문법 오류로 알림이 안 뜬다)."""
        calls = []

        def run(args, **kw):
            calls.append(args)
            if len(calls) == 1:
                raise OSError("terminal-notifier 없음")

        with mock.patch.object(watch.shutil, "which", lambda n: None), mock.patch.object(watch.subprocess, "run", run):
            watch.notify("기다림 1", 'alice: "done"', "a", 5430)
        script = calls[1][2]
        self.assertNotIn("\\u", script)
        self.assertIn('"alice: \\"done\\""', script)  # 따옴표는 그대로 이스케이프
