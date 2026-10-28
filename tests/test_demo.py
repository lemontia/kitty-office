"""데모 모드 — 가짜 사무실이 화면이 기대하는 모양이고, 답을 보내면 잠깐 일하다 보고한다."""
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone

from office import demo

NOW = datetime(2026, 10, 28, 10, 0, tzinfo=timezone(timedelta(hours=9)))


class TestDemo(unittest.TestCase):
    def setUp(self):  # 띄워 둔 데모 서버의 할 일 파일을 건드리지 않게
        self.tmp = tempfile.TemporaryDirectory()
        self.old, demo.TODO_PATH = demo.TODO_PATH, Path(self.tmp.name) / "todo.json"

    def tearDown(self):
        demo.TODO_PATH = self.old
        self.tmp.cleanup()

    def test_every_state_and_long_waits(self):
        st = demo.state(NOW)
        by = {s["name"]: s for s in st["sessions"]}
        self.assertEqual({s["state"] for s in st["sessions"]}, {"waiting", "working", "meeting", "reported", "done"})
        self.assertTrue(by["쇼핑몰-백엔드"]["stale"])
        self.assertEqual(by["앱-로그인"]["partner"], "서버-점검")
        self.assertEqual(set(st["cats"]), set(by))

    def test_screen_clock_is_fixed(self):  # 녹화를 다시 해도 같은 값
        a, b = demo.state(), demo.state()
        self.assertEqual(a["observedAt"][11:19], "14:00:00")
        self.assertEqual([s["waitingSince"] for s in a["sessions"]], [s["waitingSince"] for s in b["sessions"]])

    def test_answer_works_then_reports(self):
        demo.answer("d1", "응 올려 줘", NOW)
        s = next(x for x in demo.state(NOW, NOW + timedelta(seconds=1))["sessions"] if x["id"] == "d1")
        self.assertEqual(s["state"], "working")
        s = next(x for x in demo.state(NOW, NOW + timedelta(seconds=demo.WORK_SEC + 1))["sessions"] if x["id"] == "d1")
        self.assertEqual(s["state"], "reported")
        self.assertEqual([r["who"] for r in s["recent"][-2:]], ["me", "ai"])
        demo.reset()
        s = next(x for x in demo.state(NOW)["sessions"] if x["id"] == "d1")
        self.assertEqual(s["state"], "waiting")                                   # 새로 열면 처음 상태


if __name__ == "__main__":
    unittest.main()
