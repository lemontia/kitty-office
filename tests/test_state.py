"""상태 판정 테스트 — 임시 디렉터리에 가짜 ~/.claude 를 만들어 build_state 로 검사한다."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from office.state import build_state, is_human_line, is_question

NOW = datetime(2026, 10, 21, 1, 0, 0, tzinfo=timezone.utc)
# 가짜 설정 — 실제 사용자 설정(~/.config/kitty-office · 옛 knock-office)은 읽지 않는다
CFG = {
    "title": "테스트 사무실",
    "floors": [
        {"id": "content", "name": "글쓰기", "order": 2, "prefixes": ["~/영상편집", "~/work/blog"]},
        {"id": "hq", "name": "본부", "order": 1, "prefixes": ["~/work/hq"]},
        {"id": "lobby", "name": "로비", "order": -1, "prefixes": []},
    ],
    "seats": {"~/work/hq": "alice", "~/work/hq/lab": "bob"},
    "names": {"연구": "bob", "총괄": "alice"},
    "notify": False, "allowedHosts": [], "token": None,
}


def ts(**ago):
    return (NOW - timedelta(**ago)).isoformat().replace("+00:00", "Z")


def human(text, **ago):
    return {"type": "user", "message": {"role": "user", "content": text}, "timestamp": ts(**ago)}


def said(text, **ago):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
            "timestamp": ts(**ago)}


def tool_use(name, inp, tid="toolu_1", **ago):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": name, "input": inp}]}, "timestamp": ts(**ago)}


def tool_result(tid="toolu_x", **ago):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}, "timestamp": ts(**ago)}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / ".claude/sessions").mkdir(parents=True)
        (self.home / ".claude/projects/-x").mkdir(parents=True)
        self.n = 0

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, name, lines=None, status="idle", cwd=None, pid=None, entrypoint="cli", started=None, raw=None, extra=None):
        self.n += 1
        sid = f"sid-{self.n}"
        cwd = cwd or str(self.home / "git/elsewhere")
        sess = {"pid": pid or os.getpid(), "sessionId": sid, "cwd": cwd, "name": name, "status": status,
                "entrypoint": entrypoint, "startedAt": started or 1000 + self.n,
                "messagingSocketPath": f"/tmp/cc-socks/{1000 + self.n}.sock", **(extra or {})}
        (self.home / f".claude/sessions/{1000 + self.n}.json").write_text(json.dumps(sess, ensure_ascii=False))
        if lines is not None or raw is not None:
            body = "".join(json.dumps(l, ensure_ascii=False) + "\n" for l in (lines or []))
            (self.home / f".claude/projects/-x/{sid}.jsonl").write_text(body + (raw or ""))
        return sid

    def state(self):
        return build_state(self.home, NOW, CFG)

    def one(self, name):
        found = [s for s in self.state()["sessions"] if s["name"] == name]
        self.assertEqual(len(found), 1, f"{name} 세션이 하나여야 함")
        return found[0]


class TestState(Base):
    def test_status_waiting_is_waiting(self):
        for wf in ("approve Bash: ls", "input needed"):
            self.add("w" + wf[:3], [human("해 줘", minutes=20), said("작업을 마쳤습니다.", minutes=10)],
                     status="waiting", extra={"waitingFor": wf})
            s = self.one("w" + wf[:3])
            self.assertEqual((s["state"], s["stale"]), ("waiting", False))
            self.assertIn(wf, s["lastText"])

    def test_background_shell_is_not_working(self):
        self.add("a", [human("올려 둘까?", minutes=30), said("1과 2는 진행할까요?", minutes=29)], status="shell")
        self.assertEqual(self.one("a")["state"], "waiting")

    def test_question_waits(self):
        self.add("a", [human("해 줘", minutes=20), said("A로 할까요?", minutes=10)])
        s = self.one("a")
        self.assertEqual((s["state"], s["stale"]), ("waiting", False))
        self.assertEqual(s["lastText"], "A로 할까요?")
        self.assertIsNotNone(s["waitingSince"])

    def test_question_inside_last_paragraph_waits(self):
        self.add("a", [human("해 줘", minutes=20), said("커밋하고 push할까요? push하면 운영에 배포됩니다.", minutes=10)])
        self.add("b", [human("해 줘", minutes=20), said("A로 할까요?\n\n오늘은 여기까지입니다.", minutes=10)])
        self.assertEqual(self.one("a")["state"], "waiting")
        self.assertEqual(self.one("b")["state"], "reported")

    def test_old_question_is_stale_not_off(self):
        self.add("a", [human("해 줘", hours=31), said("A로 할까요?", hours=30)])
        s = self.one("a")
        self.assertEqual((s["state"], s["stale"]), ("waiting", True))

    def test_report_goes_to_tray(self):
        self.add("a", [human("해 줘", hours=3), said("다 했습니다.", hours=2)])
        self.add("b", [human("해 줘", hours=31), said("다 했습니다.", hours=30)])
        self.assertEqual(self.one("a")["state"], "reported")
        self.assertEqual(self.one("b")["state"], "done")  # 24시간 넘은 보고는 퇴근

    def test_context_is_last_main_reply_input(self):
        def reply(inp, cread, side=False):
            r = said("네", minutes=5)
            r["message"]["usage"] = {"input_tokens": inp, "cache_creation_input_tokens": 10, "cache_read_input_tokens": cread}
            if side:
                r["isSidechain"] = True
            return r
        self.add("a", [human("해 줘", minutes=9), reply(5, 1000), reply(4, 50_000), reply(1, 900_000, side=True)])
        self.add("b", [human("해 줘", minutes=9), said("네", minutes=5)])
        self.assertEqual(self.one("a")["context"], 50_014)  # 마지막 본 대화 답 — 서브에이전트 줄은 뺀다
        self.assertIsNone(self.one("b")["context"])
        boundary = {"type": "system", "subtype": "compact_boundary", "compactMetadata": {"preTokens": 140_000, "postTokens": 31_000}, "timestamp": ts(minutes=2)}
        self.add("c", [human("해 줘", minutes=9), reply(4, 140_000), boundary])
        self.assertEqual(self.one("c")["context"], 31_000)  # 정리 뒤 아직 답이 없어도 줄어든 값

    def test_slash_command_shows_as_typed(self):
        tag = "<command-name>/clear</command-name>\n            <command-message>clear</command-message>\n            <command-args></command-args>"
        self.add("a", [human(tag, minutes=9), human("<command-name>/model</command-name><command-args>opus</command-args>", minutes=8)])
        self.assertEqual([r["text"] for r in self.one("a")["recent"]], ["/clear", "/model opus"])

    def test_pending_ask_waits(self):
        self.add("a", [human("해 줘", minutes=20),
                       tool_use("AskUserQuestion", {"questions": [{"question": "어느 쪽?"}]}, tid="ask1", minutes=10)])
        self.add("b", [human("해 줘", minutes=20),
                       tool_use("AskUserQuestion", {"questions": [{"question": "어느 쪽?"}]}, tid="ask1", minutes=10),
                       tool_result("ask1", minutes=9), said("알겠습니다. 끝냈습니다.", minutes=8)])
        self.assertEqual(self.one("a")["state"], "waiting")
        self.assertEqual(self.one("b")["state"], "reported")

    def test_non_human_lines_do_not_answer(self):
        q = said("A로 할까요?", minutes=30)
        cases = {
            "tool_result": tool_result(minutes=5),
            "cross": human('<cross-session-message from="uds:/tmp/cc-socks/1.sock" from-name="연구">\n숫자 맞음\n</cross-session-message>', minutes=5),
            # 실물 모양: 앞에 안내 문장이 붙고 isMeta
            "cross_real": dict(human('Another Claude session sent a message:\n<cross-session-message from="uds:/tmp/cc-socks/1.sock" from-name="연구">\n숫자 맞음\n</cross-session-message>', minutes=5), isMeta=True),
            "teammate": human('Another Claude session sent a message:\n<teammate-message teammate_id="w1">\n끝\n</teammate-message>', minutes=5),
            "task": human("<task-notification>\n<task-id>b1</task-id>\n</task-notification>", minutes=5),
            "meta": dict(human("Base directory for this skill: /x", minutes=5), isMeta=True),
            "reminder": human("<system-reminder>\n무엇무엇\n</system-reminder>\n  ", minutes=5),
            "compact": dict(human("This session is being continued from a previous conversation...", minutes=5), isCompactSummary=True),
            "stdout": human("<local-command-stdout>Bye!</local-command-stdout>", minutes=5),
        }
        for name, line in cases.items():
            self.add(name, [human("해 줘", minutes=40), q, line])
        for name in cases:
            self.assertEqual(self.one(name)["state"], "waiting", name)

    def test_human_reply_clears(self):
        lines = [human("해 줘", minutes=40), said("A로 할까요?", minutes=30), human("응 그렇게해", minutes=5)]
        self.add("idle", lines)
        self.add("busy", lines, status="busy")
        self.add("slash", lines[:2] + [human("<command-name>/clear</command-name>", minutes=5)])
        self.add("list", lines[:2] + [{"type": "user", "message": {"content": [
            {"type": "text", "text": "<system-reminder>x</system-reminder>"}, {"type": "text", "text": "응"}]},
            "timestamp": ts(minutes=5)}])
        # 일하는 중에 친 글은 queued_command 첨부로만 남는다(실물 확인)
        self.add("queued", lines[:2] + [{"type": "attachment", "timestamp": ts(minutes=5), "attachment": {
            "type": "queued_command", "prompt": "응 그렇게해", "origin": {"kind": "human"}}}])
        self.assertEqual(self.one("idle")["state"], "off")
        self.assertEqual(self.one("busy")["state"], "working")
        self.assertEqual(self.one("slash")["state"], "off")
        self.assertEqual(self.one("list")["state"], "off")
        self.assertEqual(self.one("queued")["state"], "off")

    def test_busy_with_recent_message_is_meeting(self):
        self.add("글쓰기", [human("물어봐", minutes=20),
                         tool_use("SendMessage", {"to": "연구", "summary": "숫자 확인", "message": "숫자 봐 줘"}, minutes=5)],
                 status="busy")
        self.add("old", [tool_use("SendMessage", {"to": "연구", "message": "옛날 일"}, minutes=30)], status="busy")
        st = self.state()
        s = next(x for x in st["sessions"] if x["name"] == "글쓰기")
        self.assertEqual((s["state"], s["partner"]), ("meeting", "연구"))
        self.assertIn({"from": "글쓰기", "to": "연구", "summary": "숫자 확인"},
                      [{k: m[k] for k in ("from", "to", "summary")} for m in st["messages"]])
        self.assertEqual(self.one("old")["state"], "working")

    def test_socket_address_resolves_and_no_duplicate(self):
        b = self.add("연구-보이스TTS", [], status="busy")
        sock = "uds:/tmp/cc-socks/1001.sock"  # 첫 세션(연구)의 소켓
        self.add("글쓰기", [tool_use("SendMessage", {"to": sock, "summary": "확인"}, minutes=3)], status="busy")
        # 받는 쪽 기록에도 같은 메시지가 남는다
        p = self.home / f".claude/projects/-x/{b}.jsonl"
        p.write_text(json.dumps(dict(human('Another Claude session sent a message:\n<cross-session-message from="uds:/tmp/cc-socks/1002.sock" from-name="글쓰기">\n확인\n</cross-session-message>', minutes=3), isMeta=True), ensure_ascii=False) + "\n")
        st = self.state()
        self.assertEqual(self.one("글쓰기")["partner"], "연구-보이스TTS")
        self.assertEqual(self.one("연구-보이스TTS")["state"], "meeting")
        self.assertEqual(self.one("연구-보이스TTS")["partner"], "글쓰기")
        self.assertEqual(len(st["messages"]), 1)

    def test_dead_pid_and_sdk_excluded(self):
        self.add("dead", [said("A?", minutes=1)], pid=999999)
        self.add("sdk", [said("A?", minutes=1)], entrypoint="sdk-cli")
        self.add("live", [said("A?", minutes=1)])
        names = [s["name"] for s in self.state()["sessions"]]
        self.assertEqual(names, ["live"])

    def test_same_name_sessions_have_distinct_ids(self):
        # 이름이 같은 세션 둘 — 화면은 id 로 구분하므로 id 가 있고 서로 달라야 한다
        a = self.add("같은이름", [said("A?", minutes=1)])
        b = self.add("같은이름", [said("B?", minutes=2)])
        ids = [s["id"] for s in self.state()["sessions"]]
        self.assertEqual(sorted(ids), sorted([a, b]))

    def test_missing_or_broken_transcript(self):
        self.add("none_idle")
        self.add("none_busy", status="busy")
        self.add("broken", [human("해 줘", minutes=20), said("A로 할까요?", minutes=10)], raw='{"type": "assi')
        (self.home / ".claude/sessions/9999.json").write_text("{ 깨짐")
        self.assertEqual(self.one("none_idle")["state"], "off")
        self.assertEqual(self.one("none_busy")["state"], "working")
        self.assertEqual(self.one("broken")["state"], "waiting")

    def test_big_transcript_reads_tail_only(self):
        pad = said("x" * 1000, hours=5)
        self.add("big", [human("해 줘", hours=6)] + [pad] * 600 + [said("A로 할까요?", minutes=1)])
        self.assertEqual(self.one("big")["state"], "waiting")

    def test_folder_is_repo_root(self):
        repo = self.home / "git/blog"
        (repo / ".git").mkdir(parents=True)
        (repo / "web/src").mkdir(parents=True)
        self.add("a", cwd=str(repo / "web/src"))
        self.add("b", cwd=str(self.home / "notes/여행"))
        self.add("c", cwd=str(self.home))
        self.assertEqual(self.one("a")["folder"], "blog")
        self.assertEqual(self.one("b")["folder"], "여행")
        self.assertEqual(self.one("c")["folder"], "~")

    def test_floor_korean_paths(self):
        self.add("trip", cwd=str(self.home / "영상편집/1일차"))
        self.add("vault", cwd="/somewhere/else/나의문서/Obsidian Vault")
        self.add("lab", cwd=str(self.home / "work/hq/lab"))
        self.add("near", cwd=str(self.home / "work/hqq"))
        self.assertEqual(self.one("trip")["floor"], "content")
        self.assertEqual(self.one("vault")["floor"], "lobby")
        self.assertEqual(self.one("lab")["floor"], "hq")
        self.assertEqual(self.one("near")["floor"], "lobby")
        st = self.state()
        self.assertEqual(st["floors"][0]["id"], "boss")
        self.assertNotIn("prefixes", st["floors"][1])

    def test_characters(self):
        hq, lab = str(self.home / "work/hq"), str(self.home / "work/hq/lab")
        self.add("연구-보이스TTS", cwd=lab)
        self.add("hq-0b", cwd=hq, started=10)
        self.add("hq-96", cwd=hq, started=20)
        self.add("lab-2f", cwd=lab, started=1)
        self.add("hq-a1", cwd=lab, started=5)
        self.add("내가붙인이름", cwd=hq, started=1)
        self.assertEqual(self.one("연구-보이스TTS")["character"], "bob")
        # 이름을 안 붙인 세션은 자리 주인 이름-번호(시작 순)로 바뀐다
        self.assertEqual(self.one("총괄-1")["character"], "alice")
        self.assertEqual(self.one("총괄-2")["character"], "alice")
        self.assertEqual(self.one("연구-1")["character"], "bob")
        # 붙인 이름과, 폴더명이 다른 자동 이름은 그대로 둔다
        self.one("내가붙인이름")
        self.one("hq-a1")
        self.assertEqual(self.one("총괄-1")["realName"], "hq-0b")

    def test_empty(self):
        st = self.state()
        self.assertEqual(st["sessions"], [])
        self.assertEqual(st["messages"], [])


class TestQuestion(unittest.TestCase):
    def test_real_endings(self):
        # 10-21 실제 세션 끝 문단에서 가져온 모양
        self.assertTrue(is_question("감시기 수정은 아직 커밋하지 않았습니다. 커밋하고 push할까요? push하면 운영에 배포됩니다."))
        self.assertTrue(is_question("정리했습니다.\n\n`Downloads` 쪽 사본을 지울지, 이대로 커밋할지 알려 주세요."))
        self.assertTrue(is_question("고칠 곳은 '몇 분 몇 초'로 말씀해 주시면 됩니다. 확인이 끝나면 렌더하겠습니다."))
        self.assertTrue(is_question("1번만 해 주시면 나머지는 제가 이어 가겠습니다."))
        self.assertFalse(is_question("어느 쪽으로 할까요?\n\n둘 다 급하지 않습니다. 오늘은 여기까지입니다."))
        self.assertFalse(is_question("빌드는 통과했습니다. 커밋은 아직 안 했습니다."))
        # 물음표 없이 결정을 바라는 마지막 문단
        self.assertTrue(is_question("1. 메뉴에서 뺍니다.\n2. 이름을 바꿉니다.\n\n제 추천은 1번입니다. 월별 추이는 매장명 클릭으로 들어갈 수 있기 때문입니다."))
        self.assertTrue(is_question("A안과 B안이 있습니다. 어느 쪽이든 오늘 안에 됩니다."))
        self.assertTrue(is_question("이대로 확정하시면 README에 남기고 각 담당에게 넘기겠습니다."))
        self.assertTrue(is_question("앞서 여쭌 것도 아직 답을 기다리고 있습니다."))
        self.assertFalse(is_question(""))


class TestHumanLine(unittest.TestCase):
    def test_rules(self):
        self.assertTrue(is_human_line({"type": "user", "message": {"content": "응"}}))
        self.assertFalse(is_human_line({"type": "user", "message": {"content": "   "}}))
        self.assertFalse(is_human_line({"type": "assistant", "message": {"content": "응"}}))
        self.assertFalse(is_human_line({"type": "user", "isMeta": True, "message": {"content": "응"}}))
        self.assertFalse(is_human_line({"type": "user", "message": {"content": "<local-command-caveat>x</local-command-caveat>"}}))


if __name__ == "__main__":
    unittest.main()


class TestTailCache(Base):
    def test_unchanged_transcript_is_not_reread(self):
        from unittest import mock
        import office.state as st
        sid = self.add("캐시", [said("A로 할까요?", minutes=1)])
        with mock.patch.object(st, "read_tail", wraps=st.read_tail) as rt:
            self.state(); self.state()
            self.assertEqual(rt.call_count, 1)
            path = self.home / f".claude/projects/-x/{sid}.jsonl"
            with open(path, "a") as f:
                f.write(json.dumps(human("응 A", minutes=0), ensure_ascii=False) + "\n")
            self.assertEqual(self.one("캐시")["state"], "off")
            self.assertEqual(rt.call_count, 2)


class TestSignoff(Base):
    def test_short_signoff_is_done_not_report(self):
        self.add("인사", [human("고마워 수고했어", minutes=3), said("네, 수고하셨어요. 필요하면 언제든 불러 주세요.", minutes=2)])
        self.add("보고", [human("해 줘", minutes=3), said("빌드는 통과했습니다. 커밋은 아직 안 했습니다.", minutes=2)])
        self.add("긴보고", [human("해 줘", minutes=3), said("정리 끝났습니다. " + "내용 " * 40 + "수고하셨어요.", minutes=2)])
        self.assertEqual(self.one("인사")["state"], "done")
        self.assertEqual(self.one("보고")["state"], "reported")
        self.assertEqual(self.one("긴보고")["state"], "reported")


class TestRecent(Base):
    def test_last_three_replies_with_my_lines(self):
        self.add("대화", [human("하나", minutes=9), said("답1", minutes=8),
                          human("둘", minutes=7), said("중간", minutes=6.5), said("답2", minutes=6),
                          human("셋", minutes=5), said("답3", minutes=4),
                          human("넷", minutes=3), said("답4?", minutes=2)])
        r = self.one("대화")["recent"]
        self.assertEqual([(x["who"], x["text"]) for x in r],
                         [("me", "둘"), ("ai", "답2"), ("me", "셋"), ("ai", "답3"), ("me", "넷"), ("ai", "답4?")])


    def test_progress_last_text_is_whole(self):
        long = "가" * 500
        self.add("길게", [human("해 줘", minutes=5), said(long, minutes=4), said(long + "끝", minutes=3)])
        g = self.one("길게")["progress"]
        self.assertEqual(g[0]["text"], "가" * 400 + "…")  # 앞의 중간 글은 짧게
        self.assertEqual(g[1]["text"], long + "끝")  # 마지막 글은 표가 잘리지 않게 다
        self.assertNotIn("full", g[1])

    def test_progress_is_work_after_last_human(self):
        self.add("진행", [human("먼저 한 말", minutes=9), said("옛 답", minutes=8.5), tool_use("Read", {"file_path": "/a/old.py"}, "t0", minutes=8.4),
                         human("이거 고쳐 줘", minutes=5), said("파일부터 봅니다", minutes=4.5),
                         tool_use("Read", {"file_path": "/repo/web/index.html"}, "t1", minutes=4.4), tool_result("t1", minutes=4.3),
                         tool_use("Bash", {"command": "python3 -m unittest", "description": "테스트 돌리기"}, "t2", minutes=4), tool_result("t2", minutes=3.9),
                         said("고치는 중입니다", minutes=3)])
        s = self.one("진행")
        self.assertEqual([(g["kind"], g.get("name"), g["text"]) for g in s["progress"]],
                         [("text", None, "파일부터 봅니다"), ("tool", "Read", "index.html"), ("tool", "Bash", "테스트 돌리기"), ("text", None, "고치는 중입니다")])
        self.assertEqual(s["lastHumanAt"], ts(minutes=5).replace("Z", "+00:00"))
        self.assertEqual([r["text"] for r in s["recent"]][-2:], ["이거 고쳐 줘", "고치는 중입니다"])  # recent 는 그대로(사람 말 사이 마지막 글)

    def test_reads_further_back_when_tail_is_big_tool_output(self):
        big = {"type": "user", "timestamp": ts(minutes=1.5),
               "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "x" * 500_000}]}}
        self.add("캡처", [human("하나", minutes=9), said("답1", minutes=8), human("둘", minutes=7), said("답2", minutes=6),
                          human("셋", minutes=3), big, said("답3", minutes=1)])
        r = self.one("캡처")["recent"]
        self.assertEqual([x["text"] for x in r if x["who"] == "ai"], ["답1", "답2", "답3"])

class TestSubagentPeer(Base):
    def test_subagent_id_shows_as_subagent(self):
        self.add("메인", [human("해 줘", minutes=3),
                          {"type": "assistant", "timestamp": ts(minutes=1), "message": {"content": [
                              {"type": "tool_use", "name": "SendMessage", "input": {"to": "adb3ea964333ad246", "summary": "추가 요청", "message": "x"}}]}}],
                 status="busy")
        s = self.one("메인")
        self.assertEqual((s["state"], s["partner"]), ("meeting", "서브에이전트"))


class TestDone(Base):
    def test_my_wrap_up_makes_done_even_if_reply_is_long(self):
        long = "네, 수고하셨습니다. 오늘 작업은 모두 운영에 배포했습니다.\n\n다음에 이어서 볼 만한 것만 적어 둡니다.\n- " + "옛 화면 " * 30
        self.add("마무리", [human("다 됐어?", minutes=20), said("배포했습니다.", minutes=19),
                            human("응 수고햇어. 세셧 닫는다?", minutes=3), said(long, minutes=2)])
        self.add("이어감", [human("고쳐 줘", minutes=3), said("고쳤습니다. " + "내용 " * 40, minutes=2)])
        self.add("갓 띄움", [])
        self.assertEqual(self.one("마무리")["state"], "done")
        self.assertEqual(self.one("이어감")["state"], "reported")
        self.assertEqual(self.one("갓 띄움")["state"], "off")
