"""데모 모드 — 실제 세션 대신 가짜 사무실을 보여 준다(영상·발표용). KITTY_OFFICE_DEMO=1 로 켠다.
실제 세션 기록·설정 파일·터미널은 읽지도 쓰지도 않는다. 할 일은 임시 폴더, 답 보내기는 화면 안에서만 흉내 낸다."""
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path

from office import todo
from office.state import BOSS_FLOOR, VERSION

TODO_PATH = Path(tempfile.gettempdir()) / "kitty-office-demo" / "todo.json"  # 실제 할 일(설정 폴더)과 따로
WORK_SEC = 8  # 답을 보내면 이만큼 일하는 척하다가 보고한다


def demo_at():
    """화면의 「지금」 — 오늘 오후 2시로 고정. 녹화를 몇 번 해도 기다린 시간·머리줄 시각·밤낮이 같다(화면이 이 시각에 시계를 맞춘다)."""
    return datetime.now().astimezone().replace(hour=14, minute=0, second=0, microsecond=0)

LOCK = threading.Lock()
SENT = {}  # 세션 id → [(보낸 시각, 글)] — 화면에서 보낸 답

# (id, 이름, 폴더, 털, 상태, 몇 분 전, 마지막 AI 글, 덧붙임)
SESSIONS = [
    ("d1", "블로그-작가", "blog", "ginger", "waiting", 11 * 60, "초안 두 편 써 뒀습니다. 내일 아침 8시에 예약으로 올릴까요?", {}),
    ("d2", "쇼핑몰-백엔드", "shop-api", "tuxedo", "waiting", 3 * 24 * 60, "결제 취소를 두 가지로 만들 수 있습니다.\n1. 전액 환불만\n2. 부분 환불까지\n어느 쪽으로 할까요?", {"stale": True}),
    ("d3", "랜딩-디자인", "landing", "calico", "waiting", 6, "승인·선택 대기: Bash(npm run build)", {"dialog": True}),
    ("d4", "데이터-정리", "report", "siamese", "working", 0, None, {"progress": [("tool", "Read", "sales-2026-10.csv"), ("text", None, "10월 매출을 지역별로 묶는 중입니다."), ("tool", "Bash", "python3 sum_by_region.py")]}),
    ("d5", "앱-로그인", "app", "mackerel", "meeting", 0, None, {"partner": "서버-점검"}),
    ("d6", "서버-점검", "infra", "gray", "meeting", 0, None, {"partner": "앱-로그인"}),
    ("d7", "뉴스레터", "mail", "cream", "reported", 20, "이번 주 뉴스레터 다 썼습니다. 미리보기 링크 붙여 둘게요.", {}),
    ("d8", "가계부-봇", "ledger", "brown", "reported", 2 * 60 + 10, "10월 지출 정리 끝. 카페가 지난달보다 30% 늘었어요.", {}),
    ("d9", "번역-도우미", "translate", "black", "reported", 9 * 60, "설명서 영어판 번역 끝났습니다.", {}),
    ("d10", "회의록", "notes", "patch", "done", 30 * 60, "오늘 회의록 정리했습니다. 수고하셨어요!", {}),
]


def _iso(t):
    return t.isoformat(timespec="seconds")


def seed_todo(now=None):
    """시작할 때 한 번 — 칠판에 가짜 할 일."""
    now = now or datetime.now().astimezone()
    if TODO_PATH.exists():
        return
    TODO_PATH.parent.mkdir(parents=True, exist_ok=True)
    for text in ("블로그 초안 검토", "결제 취소 방향 정하기", "디자인 시안 피드백", "세금계산서 발행"):
        todo.act({"op": "add", "text": text}, TODO_PATH, now)
    first = todo.load(TODO_PATH, now)[2]["id"]
    todo.act({"op": "toggle", "id": first}, TODO_PATH, now)


def reset():
    """처음 상태로 — 보낸 답·할 일 체크를 지운다. 데모 화면을 새로 열 때마다 부른다(녹화를 몇 번 해도 같은 출발)."""
    with LOCK:
        SENT.clear()
    TODO_PATH.unlink(missing_ok=True)
    seed_todo()


def answer(sid, text, real=None):
    """화면에서 보낸 답 — 대화에 붙이고 잠깐 일하는 중으로. 일한 시간은 진짜 시계로 잰다."""
    with LOCK:
        SENT.setdefault(sid, []).append((real or datetime.now().astimezone(), str(text)[:2000]))
    return {"ok": True}


def state(now=None, real=None):
    """now 는 화면의 고정 시각(demo_at), real 은 보낸 답이 몇 초 지났나 재는 진짜 시각."""
    now, real = now or demo_at(), real or datetime.now().astimezone()
    ago = lambda m: now - timedelta(minutes=m)
    out, cats = [], {}
    with LOCK:
        sent = {k: list(v) for k, v in SENT.items()}
    for sid, name, folder, coat, st, mins, text, ex in SESSIONS:
        cats[name] = coat
        at = ago(mins)
        recent = [{"who": "me", "at": _iso(at - timedelta(minutes=3)), "text": "이거 부탁해"}]
        if text and st != "working":
            recent.append({"who": "ai", "at": _iso(at), "text": text})
        prog = [{"kind": k, "name": n, "text": t, "at": _iso(now - timedelta(seconds=40 - i * 15))} if n else {"kind": k, "text": t, "at": _iso(now - timedelta(seconds=40 - i * 15))}
                for i, (k, n, t) in enumerate(ex.get("progress", []))]
        last, since, stale = at, at if st == "waiting" else None, ex.get("stale", False)
        for t_sent, msg in sent.get(sid, []):  # 보낸 답 — 몇 초 일하는 척하고 보고로
            recent.append({"who": "me", "at": _iso(now), "text": msg})  # 화면 시각으로 — 고정 시각이라 「방금」
            done = (real - t_sent).total_seconds() >= WORK_SEC
            st, since, stale, last = ("reported", None, False, now) if done else ("working", None, False, now)
            text = "알겠습니다. 말씀하신 대로 해 두었습니다." if done else None
            prog = [] if done else [{"kind": "text", "text": "말씀하신 대로 고치는 중입니다.", "at": _iso(now)}]
            if done:
                recent.append({"who": "ai", "at": _iso(last), "text": text})
        out.append({
            "id": sid, "name": name, "realName": name, "floor": "lobby", "folder": folder, "island": folder, "color": None,
            "character": name, "state": st, "stale": stale, "waitingSince": _iso(since) if since else None,
            "lastText": text[-120:] if text else None, "fullText": text, "recent": recent, "context": 60_000,
            "progress": prog, "lastHumanAt": _iso(at - timedelta(minutes=3)), "tab": True, "canFocus": False,
            "dialog": bool(ex.get("dialog")) and st == "waiting", "partner": ex.get("partner") if st == "meeting" else None,
            "lastActivity": _iso(now if st in ("working", "meeting") else last),
        })
    log = [{"at": _iso(ago(2)), "from": "앱-로그인", "to": "서버-점검", "summary": "로그인 오류 로그 같이 보자"},
           {"at": _iso(ago(1)), "from": "서버-점검", "to": "앱-로그인", "summary": "토큰 만료 시간이 짧게 잡혀 있었어"}]
    return {"title": "Kitty Office", "version": VERSION, "observedAt": _iso(now), "demo": True,
            "floors": [BOSS_FLOOR, {"id": "lobby", "name": "로비", "order": -1}], "sessions": out, "messages": log,
            "cats": cats, "weather": None, "todo": todo.summary(TODO_PATH, now)}
