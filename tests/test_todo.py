"""오늘 할 일 — 날짜가 바뀌면 정리, 상한, 원자적 쓰기. 시계는 주입한다."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from office import todo

KST = timezone(timedelta(hours=9))
D1 = datetime(2026, 10, 25, 9, 0, tzinfo=KST)


class TestTodo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Path(self.tmp.name) / "todo.json"

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, text, now=D1):
        return todo.act({"op": "add", "text": text}, self.p, now)[-1]["id"]

    def test_done_kept_30_days_then_dropped(self):
        a, b = self.add("끝낸 일"), self.add("못 한 일")
        todo.act({"op": "toggle", "id": a}, self.p, D1)
        self.assertEqual(todo.summary(self.p, D1 + timedelta(hours=3)), {"done": 1, "total": 2, "late": 0, "past": 0})  # 오늘 끝낸 건 오늘 목록에
        nxt = todo.load(self.p, D1 + timedelta(days=1))
        self.assertEqual([(i["id"], i["past"]) for i in nxt], [(a, True), (b, False)])                     # 다음 날엔 지난 기록(접힘)
        self.assertEqual(todo.summary(self.p, D1 + timedelta(days=1)), {"done": 0, "total": 1, "late": 0, "past": 1})
        self.assertEqual(len(todo.load(self.p, D1 + timedelta(days=30))), 2)                                # 30일째까지 보관
        self.assertEqual([i["id"] for i in todo.load(self.p, D1 + timedelta(days=31))], [b])               # 31일째 지운다
        self.assertEqual([i["id"] for i in json.loads(self.p.read_text(encoding="utf-8"))["items"]], [b])  # 파일에도 반영

    def test_undo_past_done_keeps_created(self):
        a = self.add("되돌릴 일")
        created = todo.load(self.p, D1)[0]["created"]
        todo.act({"op": "toggle", "id": a}, self.p, D1)
        it = todo.act({"op": "toggle", "id": a}, self.p, D1 + timedelta(days=2))[0]
        self.assertEqual((it["done"], it["doneAt"], it["created"], it["past"]), (False, None, created, False))

    def test_limits_count_undone_only_and_cap_done_records(self):
        for i in range(todo.ITEMS_MAX):
            todo.act({"op": "toggle", "id": self.add(f"끝낸 {i}")}, self.p, D1)
        self.add("끝낸 기록이 많아도 추가된다")                                                         # 안 끝난 것만 센다
        with mock.patch.object(todo, "DONE_MAX", 30):
            items = todo.load(self.p, D1 + timedelta(minutes=1))
        self.assertEqual(sum(1 for i in items if i["done"]), 30)
        self.assertEqual(sum(1 for i in items if not i["done"]), 1)

    def test_old_undone_is_marked(self):
        self.add("오래된 일")
        self.assertFalse(todo.load(self.p, D1 + timedelta(days=2))[0]["old"])
        self.assertTrue(todo.load(self.p, D1 + timedelta(days=3))[0]["old"])

    def test_late_items_fold_and_come_back_today(self):
        a, b = self.add("밀린 일"), self.add("오늘 일", D1 + timedelta(days=7))
        now = D1 + timedelta(days=7, hours=1)
        late = {i["id"]: i["late"] for i in todo.load(self.p, now)}
        self.assertEqual(late, {a: True, b: False})
        self.assertEqual(todo.summary(self.p, now), {"done": 0, "total": 1, "late": 1, "past": 0})  # 밀린 일은 세기에서 뺀다
        items = todo.act({"op": "today", "id": a}, self.p, now)
        self.assertEqual([i["id"] for i in items], [b, a])                               # 맨 아래로
        self.assertFalse(items[-1]["late"]); self.assertFalse(items[-1]["old"])
        self.assertEqual(todo.summary(self.p, now), {"done": 0, "total": 2, "late": 0, "past": 0})
        with self.assertRaises(todo.TodoError):
            todo.act({"op": "today", "id": "nope"}, self.p, now)

    def test_limits(self):
        with self.assertRaises(todo.TodoError):
            self.add("x" * (todo.TEXT_MAX + 1))
        self.add("x" * todo.TEXT_MAX)
        for i in range(todo.ITEMS_MAX - 1):
            self.add(f"할 일 {i}")  # 안 끝난 항목 100개
        with self.assertRaises(todo.TodoError) as e:
            self.add("하나 더")
        self.assertEqual(str(e.exception), "todoFull")

    def test_atomic_write_keeps_old_file_on_failure(self):
        self.add("남아야 할 일")
        before = self.p.read_text(encoding="utf-8")
        with mock.patch.object(Path, "replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.add("실패할 일")
        self.assertEqual(self.p.read_text(encoding="utf-8"), before)  # 임시 파일에 쓰고 바꿔 끼우므로 원본은 그대로

    def test_broken_or_missing_file_is_empty(self):
        self.assertEqual(todo.load(self.p, D1), [])
        self.p.write_text("not json", encoding="utf-8")
        self.assertEqual(todo.load(self.p, D1), [])


if __name__ == "__main__":
    unittest.main()
