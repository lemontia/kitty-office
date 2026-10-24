"""세션 끝내기(term.exit_session) — subprocess 를 가짜로 바꿔 부르는 순서만 본다. 실제 터미널은 건드리지 않는다."""
import unittest
from unittest import mock

from office import term


class TestExitSession(unittest.TestCase):
    def run_exit(self, tab, alive=False, fail=()):
        calls = []

        def fake_run(*args):
            calls.append(args[1:])
            if any(f in args for f in fail):
                raise RuntimeError("live process")
            return ""
        with mock.patch.object(term, "_run", fake_run), mock.patch.object(term, "_alive", lambda pid: alive), \
             mock.patch.object(term.time, "sleep", lambda s: None), mock.patch.object(term, "CMUX", "cmux"), \
             mock.patch.object(term, "ORCA", "orca"), mock.patch.object(term, "HERDR", "herdr"):
            note = term.exit_session(tab, pid=4242, wait=0.01)
        return calls, note

    def test_cmux_paste_exit_then_close_that_surface_only(self):
        calls, note = self.run_exit(("cmux", "ws1", "sf9"))
        self.assertEqual(calls, [("paste", "--workspace", "ws1", "--surface", "sf9", "--submit", "--", "/exit"),
                                 ("close-surface", "--workspace", "ws1", "--surface", "sf9")])
        self.assertIsNone(note)
        self.assertFalse(any("close-workspace" in c for c in calls))

    def test_cmux_force_only_after_claude_gone(self):
        calls, note = self.run_exit(("cmux", "ws1", "sf9"), fail=("close-surface",))
        self.assertEqual(calls[-1], ("close-surface", "--workspace", "ws1", "--surface", "sf9", "--force"))

    def test_still_alive_keeps_tab(self):
        calls, note = self.run_exit(("cmux", "ws1", "sf9"), alive=True)
        self.assertEqual([c[0] for c in calls], ["paste"])  # 프로세스가 안 끝났으면 탭은 안 닫는다
        self.assertEqual(note, "still_running")

    def test_orca_and_herdr_close_one_pane(self):
        self.assertEqual(self.run_exit(("orca", "term_1"))[0][-1], ("terminal", "close", "--terminal", "term_1"))
        self.assertEqual(self.run_exit(("herdr", "p3"))[0][-1], ("pane", "close", "p3"))


class TestAnswer(unittest.TestCase):
    def test_cmux_text_after_double_dash(self):
        """「-v」·「-」 같은 답이 옵션이나 stdin 표시로 읽히지 않게 cmux paste 는 -- 뒤에 글을 둔다(cmux paste --help 의 [--])."""
        calls = []
        with mock.patch.object(term, "_run", lambda *a: calls.append(a[1:]) or ""), mock.patch.object(term, "CMUX", "cmux"):
            term.answer(("cmux", "ws1", "sf9"), "-v")
        self.assertEqual(calls, [("paste", "--workspace", "ws1", "--surface", "sf9", "--submit", "--", "-v")])


if __name__ == "__main__":
    unittest.main()
