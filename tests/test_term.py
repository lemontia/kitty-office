"""세션 끝내기(term.exit_session) — subprocess 를 가짜로 바꿔 부르는 순서만 본다. 실제 터미널은 건드리지 않는다."""
import ctypes  # noqa: F401 — os.name 을 nt 로 바꾸기 전에 미리 불러 둔다
import json
import os
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
        with mock.patch.object(term, "_run", fake_run), mock.patch.object(term, "alive", lambda pid: alive), \
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


class AliveTest(unittest.TestCase):
    def test_windows_never_calls_os_kill(self):
        # 윈도우에서 os.kill(pid, 0) 은 그 프로세스를 끈다 — 살아 있나 볼 때 절대 부르지 않는다
        k = mock.Mock()
        k.OpenProcess.return_value = 7
        k.GetExitCodeProcess.side_effect = lambda h, ref: (setattr(ref._obj, "value", 259), 1)[1]
        boom = mock.Mock(side_effect=AssertionError("os.kill called"))
        with mock.patch.object(term.os, "name", "nt"), mock.patch.object(term.os, "kill", boom), \
                mock.patch("ctypes.windll", mock.Mock(kernel32=k), create=True):
            self.assertTrue(term.alive(1234))
            k.GetExitCodeProcess.side_effect = lambda h, ref: (setattr(ref._obj, "value", 0), 1)[1]
            self.assertFalse(term.alive(1234))
            k.OpenProcess.return_value = 0
            k.GetLastError.return_value = 87  # 그런 pid 없음
            self.assertFalse(term.alive(1234))
        boom.assert_not_called()

    def test_posix_checks_without_killing(self):
        self.assertTrue(term.alive(os.getpid()))
        self.assertFalse(term.alive("x"))


class TestWindows(unittest.TestCase):
    """윈도우 — ps eww 대신 세션 파일의 sessionId 를 herdr pane list 와 맞춘다. herdr 는 가짜로."""
    def setUp(self):
        term._herdr_cache[:] = [0.0, {}]

    def test_tab_from_herdr_pane_list(self):
        import json, tempfile
        from pathlib import Path
        home = Path(tempfile.mkdtemp())
        (home / ".claude/sessions").mkdir(parents=True)
        (home / ".claude/sessions/4242.json").write_text(json.dumps({"pid": 4242, "sessionId": "abc"}), encoding="utf-8")
        listing = {"result": {"panes": [{"pane_id": "w1:p2", "agent_session": {"value": "abc"}},
                                        {"pane_id": "w1:p3", "agent_session": None}]}}
        done = mock.Mock(stdout=json.dumps(listing))
        with mock.patch.object(term, "HERDR", "herdr"), mock.patch.object(term.Path, "home", lambda: home), \
             mock.patch.object(term.subprocess, "run", lambda *a, **kw: done):
            self.assertEqual(term._tab_of_nt(4242), ("herdr", "w1:p2"))
            self.assertIsNone(term._tab_of_nt(9999))  # 세션 파일 없음

    def test_no_herdr_no_tab(self):
        with mock.patch.object(term, "HERDR", None):
            self.assertIsNone(term._tab_of_nt(4242))



class TestRunsAny(unittest.TestCase):
    PROCS = {10: (1, "claude"), 11: (10, "bash -c ..."), 12: (11, "bash rotate.sh 1"), 20: (1, "claude"), 21: (20, "yadam-studio.exe")}

    def test_finds_grandchild(self):
        with mock.patch.object(term, "_procs", lambda: self.PROCS):
            self.assertTrue(term.runs_any(10, ["rotate.sh"]))
            self.assertFalse(term.runs_any(20, ["rotate.sh"]))  # 서버만 도는 세션
            self.assertFalse(term.runs_any(10, []))


class TestClipboardPaths(unittest.TestCase):
    def ps(self, out):
        return mock.patch.object(term.subprocess, "run", lambda *a, **kw: mock.Mock(stdout=out))

    def test_reads_file_drop_list_on_windows(self):
        paths = [r"C:.png", r"C:older"]
        with mock.patch.object(term.os, "name", "nt"):
            with self.ps(json.dumps(paths)):
                self.assertEqual(term.clipboard_paths(), paths)
            with self.ps(json.dumps(paths[0])):  # 하나여도 목록으로
                self.assertEqual(term.clipboard_paths(), paths[:1])
            with self.ps(""):  # 파일 복사가 아님(캡처 이미지·글자)
                self.assertEqual(term.clipboard_paths(), [])
