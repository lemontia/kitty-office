"""세션 파일과 대화 기록 끝을 읽어 사무실 상태를 만든다. 읽기만 한다."""
import json
import os
import re
import subprocess
import unicodedata
import zlib
from datetime import datetime, timedelta
from pathlib import Path

from office import config, term

STALE_HOURS = 24
MEETING_MINUTES = 10
TAIL_BYTES = 400_000
TAIL_MAX = 8_000_000  # 끝부분에 큰 줄(캡처 이미지 등)만 있어 지난 대화가 안 잡히면 여기까지 넓혀 읽는다
MESSAGE_LIMIT = 30

BOSS_FLOOR = {"id": "boss", "name": "결재실", "order": 3}

# 사람 자리에 오지만 사람이 친 게 아닌 글의 머리
NOT_HUMAN_PREFIXES = (
    "<cross-session-message",
    "<teammate-message",
    "Another Claude session sent a message",
    "<task-notification",
    "<local-command-caveat",
    "<local-command-stdout",
    "<local-command-stderr",
)
CMD_RE = re.compile(r"<command-name>(.*?)</command-name>.*?(?:<command-args>(.*?)</command-args>|$)", re.S)


def _readable(text):
    """슬래시 명령 줄은 기록에 태그로 남는다 — 사람이 친 그대로(「/clear」)로 보여 준다."""
    m = CMD_RE.search(text)
    return f"{m.group(1)} {m.group(2) or ''}".strip() if m else text


REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
CROSS_RE = re.compile(r"<cross-session-message\b([^>]*)>(.*?)(?:</cross-session-message>|$)", re.S)
FROM_NAME_RE = re.compile(r'from-name="([^"]*)"')
# 사용자에게 무언가를 해 달라는 말(「알려 주세요」「말씀해 주시면」「1번만 해 주시면」)
ASK_RE = re.compile(r"(해|려|라)\s?주(세요|시면|십시오|실래요)")
# 물음표 없이 결정을 바라는 말(「제 추천은 1번입니다.」로 끝나는 선택지 보고 — 10-21 실물에서 보고로 새던 것)
DECIDE_RE = re.compile(r"제 추천은|추천(합니다|드립니다|해요)|어느 쪽|골라|고르시|결정해|(확정|승인|허락)하시면|답을 기다리")
# 짧은 마무리 인사(「네, 수고하셨어요. 필요하면 언제든 불러 주세요.」) — 보고가 아니라 쉬는 중
SIGNOFF_RE = re.compile(r"수고(하셨|많으셨)|언제든|좋은 (하루|밤|저녁)|편히 쉬")
SIGNOFF_MAX = 80  # 이보다 긴 글은 인사로 끝나도 내용이 있는 보고로 본다
AGENT_ID_RE = re.compile(r"a[0-9a-f]{12,20}")  # SendMessage 의 to 가 서브에이전트일 때 오는 id(예: adb3ea964333ad246)
# 사용자가 마지막에 한 말이 마무리면(「응 수고했어. 세션 닫는다?」) AI 답 길이와 상관없이 퇴근 — 10-21 실물
WRAP_RE = re.compile(r"수고(했|햇|하셨|많)|세션\s*(닫|끝)|닫(는다|을게|아도|자)|오늘은 여기까지|마무리하자|그만하자|고마워|고맙")

def seat_names(sessions, home, cfg):
    """이름을 안 붙인 세션(Claude Code 자동 이름 「폴더명-두자리」)이 자리 주인 폴더에서 떴으면
    「주인이름-번호」로 바꾼다. 번호는 시작한 순서. {옛 이름: 새 이름} 을 돌려준다."""
    display = {ch: ko for ko, ch in cfg["names"].items()}  # 캐릭터 → 표시 이름
    seats = {_expand(p, home): display[ch] for p, ch in cfg["seats"].items() if ch in display}
    alias, count = {}, {}
    # ponytail: 앞 번호 세션이 꺼지면 뒤 번호가 당겨진다. 고정 번호가 필요하면 sessionId→번호를 파일에 남긴다
    for s in sorted(sessions, key=lambda s: s.get("startedAt") or 0):
        who = seats.get(s["cwd"])
        if who and re.fullmatch(re.escape(Path(s["cwd"]).name) + r"-[0-9a-f]{2}", s["name"]):
            count[who] = count.get(who, 0) + 1
            s["realName"] = s["name"]
            alias[s["name"]] = f"{who}-{count[who]}"
            s["name"] = alias[s["name"]]
    return alias


def _nfc(s):
    return unicodedata.normalize("NFC", s or "")


def _expand(p, home):
    p = _nfc(p)
    if p == "~" or p.startswith("~/"):
        p = str(home) + p[1:]
    return p.rstrip("/") or "/"


def _time(s):
    if not isinstance(s, str):
        return None
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.astimezone()


def _text_of(content):
    """content 가 문자열이면 그대로, 리스트면 text 블록만 이어 붙인다."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _first_line(s, n=60):
    for line in (s or "").splitlines():
        if line.strip():
            return line.strip()[:n]
    return ""


def is_question(text):
    """마지막 문단에 물음표가 있거나 사용자에게 해 달라는 말이 있으면 질문으로 본다.
    실물(10-21)에선 「커밋하고 push할까요? push하면 운영에 배포됩니다.」처럼 물음 뒤에 한 문장이 더 붙는 게 흔했다."""
    paras = [p for p in re.split(r"\n\s*\n", (text or "").strip()) if p.strip()]
    if not paras:
        return False
    last = paras[-1]
    return "?" in last or "？" in last or bool(ASK_RE.search(last) or DECIDE_RE.search(last))


def _head(text, n):
    return text if len(text) <= n else text[:n].rstrip() + "…"


def is_signoff(text):
    t = (text or "").strip()
    return 0 < len(t) <= SIGNOFF_MAX and bool(SIGNOFF_RE.search(t))


def is_human_line(rec):
    """사용자가 직접 친 글(슬래시 명령 포함)인가. 도구 결과·세션 간 메시지·작업 알림·메타 줄은 아니다."""
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isCompactSummary"):
        return False
    text = _text_of((rec.get("message") or {}).get("content")).strip()
    if not text or text.startswith(NOT_HUMAN_PREFIXES):
        return False
    return bool(REMINDER_RE.sub("", text).strip())


def _is_queued_human(rec):
    """일하는 중에 친 글은 user 줄이 아니라 queued_command 첨부로만 남는다(10-21 실물 확인)."""
    a = rec.get("attachment") if rec.get("type") == "attachment" else None
    return (isinstance(a, dict) and a.get("type") == "queued_command"
            and (a.get("origin") or {}).get("kind") == "human")


def read_tail(path, n=TAIL_BYTES):
    """파일 끝 n 바이트만 읽어 줄 목록으로. 중간부터 읽었으면 잘린 첫 줄은 버린다."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - n))
        data = f.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if size > n else lines


_TAIL_CACHE = {}  # 경로 → ((수정 시각, 크기), parse_tail 결과)


def load_tail(path):
    """대화 기록을 읽어 판정 재료로. 지난번과 수정 시각·크기가 같으면 다시 읽지 않는다."""
    st = path.stat()
    key = (st.st_mtime_ns, st.st_size)
    hit = _TAIL_CACHE.get(path)
    if not hit or hit[0] != key:
        n = TAIL_BYTES
        tail = parse_tail(read_tail(path, n))
        while sum(r["who"] == "ai" for r in tail["recent"]) < RECENT_REPLIES and n < min(st.st_size, TAIL_MAX):
            n *= 4
            tail = parse_tail(read_tail(path, n))
        hit = _TAIL_CACHE[path] = (key, tail)
    # 부르는 쪽이 messages 의 peer 를 고쳐 쓰므로 사본을 준다
    return {**hit[1], "messages": [dict(m) for m in hit[1]["messages"]]}


RECENT_REPLIES = 3


def _last_replies(turns, n):
    """끝에서 AI 답 n 개가 들어가는 만큼(사이의 사람 말 포함)."""
    seen = 0
    for k in range(len(turns) - 1, -1, -1):
        if turns[k]["who"] == "ai":
            seen += 1
            if seen == n:
                return turns[max(0, k - 1):] if k and turns[k - 1]["who"] == "me" else turns[k:]
    return turns


PROGRESS_MAX = 20


def _tool_brief(name, inp):
    """도구 사용 한 줄 — Bash 는 설명(없으면 명령 앞부분), 파일 도구는 파일 이름, 찾기는 패턴."""
    inp = inp if isinstance(inp, dict) else {}
    if name == "Bash":
        return _first_line(inp.get("description") or inp.get("command") or "")
    if inp.get("file_path") or inp.get("notebook_path"):
        return Path(str(inp.get("file_path") or inp.get("notebook_path"))).name
    for k in ("description", "pattern", "url", "query", "prompt"):
        if inp.get(k):
            return _first_line(str(inp[k]))
    return ""


def parse_tail(lines):
    last_human_i = -1
    progress = []  # 마지막 사람 말 이후 AI 가 한 일 — 중간 글·도구 사용(진행 중 팝업에 쌓아 보인다)
    last_human_at = None
    last_human_text = ""
    last_text = last_text_at = None
    last_text_i = -1
    last_activity = None
    context = None  # 마지막 답을 만들 때 모델이 읽은 양(토큰) — 대화가 얼마나 찼나
    asks = {}  # AskUserQuestion id → (줄 번호, 시각, 질문)
    answered = set()
    messages = []
    turns = []  # 팝업의 「최근 대화」 — {"who": "me"|"ai", "at", "text"}. AI 쪽은 사람 말 사이의 마지막 글만

    for i, line in enumerate(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        at = _time(rec.get("timestamp"))
        if at:
            last_activity = at
        kind = rec.get("type")
        if kind == "system" and rec.get("subtype") == "compact_boundary":
            context = (rec.get("compactMetadata") or {}).get("postTokens")  # 정리(/compact) 직후 — 다음 답 전까지는 요약 뒤 크기
        content = (rec.get("message") or {}).get("content") if isinstance(rec.get("message"), dict) else None

        if is_human_line(rec) or _is_queued_human(rec):
            last_human_i, last_human_at = i, at
            t = _readable(_text_of(content).strip()) if content is not None else ""
            last_human_text = t
            progress = []
            if t:
                turns.append({"who": "me", "at": at, "text": t[:2000]})
        elif kind == "user":
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        answered.add(b.get("tool_use_id"))
            m = CROSS_RE.search(_text_of(content))
            if m:
                peer = FROM_NAME_RE.search(m.group(1))
                messages.append({"at": at, "direction": "in", "peer": peer.group(1) if peer else "",
                                 "summary": _first_line(m.group(2))})
        elif kind == "assistant" and isinstance(content, list):
            u = rec["message"].get("usage")
            if isinstance(u, dict) and not rec.get("isSidechain"):
                context = sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text", "").strip():
                    last_text, last_text_at, last_text_i = b["text"], at, i
                    if not rec.get("isSidechain"):
                        progress.append({"kind": "text", "at": at, "text": _head(b["text"].strip(), 400), "full": b["text"].strip()[:6000]})
                    item = {"who": "ai", "at": at, "text": b["text"].strip()[-6000:]}
                    if turns and turns[-1]["who"] == "ai":
                        turns[-1] = item
                    else:
                        turns.append(item)
                elif b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    if not rec.get("isSidechain"):
                        progress.append({"kind": "tool", "at": at, "name": str(b.get("name") or ""), "text": _tool_brief(b.get("name"), inp)})
                    if b.get("name") == "AskUserQuestion":
                        qs = inp.get("questions") or [{}]
                        q = qs[0].get("question", "") if isinstance(qs[0], dict) else ""
                        asks[b.get("id")] = (i, at, q)
                    elif b.get("name") == "SendMessage":
                        messages.append({"at": at, "direction": "out", "peer": str(inp.get("to", "")),
                                         "summary": inp.get("summary") or _first_line(str(inp.get("message", "")))})

    pending = [(i, at, q) for tid, (i, at, q) in asks.items() if i > last_human_i and tid not in answered]
    text_after_human = last_text is not None and last_text_i > last_human_i
    asked_in_text = text_after_human and is_question(last_text)
    question_at = question_text = None
    if pending:
        _, question_at, question_text = max(pending, key=lambda p: p[0])
    elif asked_in_text:
        question_at, question_text = last_text_at, last_text

    return {
        "last_human_at": last_human_at,
        "last_human_text": last_human_text,
        "last_assistant_text": last_text,
        "last_assistant_at": last_text_at,
        "pending_ask": bool(pending),
        "last_activity_at": last_activity,
        "messages": messages,
        "recent": _last_replies(turns, RECENT_REPLIES),
        "context": context,
        # 마지막 글은 다 보인다(표가 중간에 잘리지 않게) — 앞의 중간 글만 짧게
        "progress": [{k: v for k, v in g.items() if k != "full"} | ({"text": g["full"]} if g is progress[-1] and "full" in g else {})
                     for g in progress[-PROGRESS_MAX:]],
        # 판정에 쓰는 덧붙임
        "assistant_after_human": text_after_human,
        "question": bool(pending) or asked_in_text,
        "question_at": question_at,
        "question_text": question_text,
    }


def classify(status, tail, now, waiting_since=None):
    """(state, stale). 판정 규칙은 여기 한 곳에 모은다.
    status "waiting" 은 Claude Code 가 승인·질문 창으로 사용자를 기다린다고 직접 적은 것 — 기록과 무관하게 waiting."""
    if status == "waiting":
        at = waiting_since or tail.get("question_at")
        return "waiting", bool(at and at < now - timedelta(hours=STALE_HOURS))
    # "shell" 은 턴이 끝났는데 뒤에서 셸만 도는 상태(10-22 실물: turn_duration 과 같은 순간에 shell 로 바뀜) — 일하는 중이 아니라 기록으로 가른다
    if status == "busy":
        recent = now - timedelta(minutes=MEETING_MINUTES)
        if any(m["at"] and m["at"] >= recent for m in tail.get("messages", [])):
            return "meeting", False
        return "working", False
    stale_line = now - timedelta(hours=STALE_HOURS)
    if tail.get("question"):
        at = tail.get("question_at")
        return "waiting", bool(at and at < stale_line)
    # 퇴근(done) = 마무리했거나 24시간 넘게 움직임 없음 — 「닫아도 되는 창」. 쉬는 중(off) = 잠깐 비어 있음
    at = tail.get("last_assistant_at")
    if tail.get("assistant_after_human") and at:
        if at < stale_line or WRAP_RE.search(tail.get("last_human_text") or "") or is_signoff(tail.get("last_assistant_text")):
            return "done", False
        return "reported", False
    act = tail.get("last_activity_at")
    return ("done" if act and act < stale_line else "off"), False


def folder_of(cwd, home):
    """화면의 책상 섬 이름 — git 저장소 안이면 저장소 이름(하위 폴더에서 켜도 같은 섬), 아니면 그 폴더 이름."""
    p = Path(cwd or "?")
    for d in (p, *p.parents):
        if (d / ".git").exists():
            return d.name
        if d == home:
            break
    return "~" if p == home else p.name


def folders(home):
    """지금 떠 있는 세션들의 폴더(섬) 이름 — 서버가 상태를 만들기 전에 새 폴더 색을 정하려고 쓴다."""
    home = Path(home)
    return {folder_of(_expand(s["cwd"], home), home) for s in load_sessions(home)}


def floor_of(cwd, floors):
    """가장 긴 접두사(폴더 경계 기준)가 이긴다. 안 맞으면 lobby."""
    cwd = _expand(cwd, Path.home())
    best, best_len = "lobby", -1
    for fl in floors:
        for p in fl.get("prefixes", []):
            p = _expand(p, Path.home())
            if (cwd == p or cwd.startswith(p + "/")) and len(p) > best_len:
                best, best_len = fl["id"], len(p)
    return best


def assign_characters(sessions, home, cfg):
    out, taken = {}, set()
    for s in sessions:
        for prefix, ch in cfg["names"].items():
            if s["name"].startswith(prefix):
                out[s["name"]] = ch
                taken.add(ch)
                break
    seats = {_expand(p, home): ch for p, ch in cfg["seats"].items()}
    for s in sorted(sessions, key=lambda s: s.get("startedAt") or 0):
        ch = seats.get(_expand(s.get("cwd", ""), home))
        if s["name"] not in out and ch and ch not in taken:
            out[s["name"]] = ch
            taken.add(ch)
    for s in sessions:
        out.setdefault(s["name"], f"g{zlib.crc32(s['name'].encode()) % 4 + 1}")
    return out


def _alive(pid):
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True  # 남의 프로세스지만 살아 있다
    except (OSError, ValueError, TypeError):
        return False
    return True


def load_sessions(home):
    out = []
    for f in sorted((home / ".claude/sessions").glob("*.json")):
        try:
            s = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(s, dict) or s.get("entrypoint") == "sdk-cli" or not _alive(s.get("pid")):
            continue
        s["name"] = _nfc(s.get("name") or Path(s.get("cwd") or "?").name)
        s["cwd"] = _nfc(s.get("cwd"))
        out.append(s)
    return out


def _transcript(home, sid):
    if not sid:
        return None
    found = list((home / ".claude/projects").glob(f"*/{sid}.jsonl"))
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def _iso(t):
    return t.isoformat(timespec="seconds") if t else None


def _version():
    """지금 돌고 있는 코드의 커밋 — 서버가 뜰 때 한 번 읽는다(배포가 반영됐나 설정 창에서 본다)."""
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%h · %cd", "--date=format:%m-%d %H:%M"],
                             cwd=Path(__file__).resolve().parent, capture_output=True, text=True, timeout=3)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


VERSION = _version()


def build_state(home, now, cfg=None):
    home = Path(home)
    cfg = cfg or config.load()
    floors = [{**fl, "prefixes": [_expand(p, home) for p in fl.get("prefixes", [])]} for fl in cfg["floors"]]
    sessions = load_sessions(home)
    alias = seat_names(sessions, home, cfg)
    chars = assign_characters(sessions, home, cfg)
    # SendMessage 의 to 가 소켓 주소일 때 세션 이름으로 바꾼다
    by_sock = {"uds:" + s["messagingSocketPath"]: s["name"] for s in sessions if s.get("messagingSocketPath")}
    names = {s["name"] for s in sessions}

    out, log = [], []
    for s in sessions:
        path = _transcript(home, s.get("sessionId"))
        try:
            tail = load_tail(path) if path else parse_tail([])
        except OSError:
            tail = parse_tail([])
        for m in tail["messages"]:
            m["peer"] = alias.get(m["peer"], by_sock.get(m["peer"], m["peer"]))
            if AGENT_ID_RE.fullmatch(m["peer"]):  # 세션이 띄운 작업자(서브에이전트)의 내부 번호
                m["peer"] = "서브에이전트"
        ms = s.get("statusUpdatedAt")
        since_f = datetime.fromtimestamp(ms / 1000, now.tzinfo) if isinstance(ms, (int, float)) else None
        state, stale = classify(s.get("status"), tail, now, since_f)
        partner = None
        if state == "meeting":
            partner = max((m for m in tail["messages"] if m["at"]), key=lambda m: m["at"])["peer"]
        if state == "waiting":
            text, since = tail["question_text"], since_f or tail["question_at"]
            if not text and s.get("status") == "waiting":
                text = "승인·선택 대기: " + str(s.get("waitingFor") or "")
        else:
            text, since = tail["last_assistant_text"], None
        out.append({
            "id": s.get("sessionId"),
            "name": s["name"],
            "realName": s.get("realName", s["name"]),  # Claude Code 가 붙인 진짜 이름(메시지 주소)
            "floor": floor_of(s["cwd"], floors),
            "folder": (fd := folder_of(_expand(s["cwd"], home), home)),
            "island": cfg.get("islands", {}).get(fd) or fd,  # 화면 섬 이름 — 설정에서 고쳤으면 그 이름
            "color": cfg.get("colors", {}).get(fd),  # 목록 색 — 서버가 폴더마다 정해 설정에 적어 둔 것(config.assign_colors). 없으면 화면이 해시 색
            "character": chars[s["name"]],
            "state": state,
            "stale": stale,
            "waitingSince": _iso(since),
            "lastText": text.strip()[-120:] if text else None,
            "fullText": text.strip()[-6000:] if text else None,
            "recent": [{"who": r["who"], "at": _iso(r["at"]), "text": r["text"]} for r in tail.get("recent", [])],  # 목록을 눌렀을 때 펼쳐 보는 전체 메시지
            "context": tail.get("context"),
            # 마지막 사람 말 이후 AI 가 한 일(중간 글·도구) — 진행 중 팝업에 보인다. recent 와 별개
            "progress": [{**{k: v for k, v in g.items() if k != "at"}, "at": _iso(g["at"])} for g in tail.get("progress", [])],
            "lastHumanAt": _iso(tail.get("last_human_at")),  # 대화가 찬 양(토큰). 한도는 모델·설정마다 달라 화면이 판단
            "tab": bool(term.tab_of(s.get("pid"))),  # cmux·Orca·herdr 에서 떴나 — 팝업에서 답하기
            "canFocus": term.can_focus(term.tab_of(s.get("pid"))),  # 팝업에서 그 창으로 가기
            "dialog": s.get("status") == "waiting",  # 승인·선택 창 — 답하기 대신 탭으로 가기만
            "partner": partner,
            "lastActivity": _iso(tail["last_activity_at"]),
        })
        for m in tail["messages"]:
            # 화면에 있는 세션끼리 주고받은 건 보낸 쪽 기록으로만 센다(두 번 안 세게)
            if m["direction"] == "in" and m["peer"] in names:
                continue
            frm, to = (s["name"], m["peer"]) if m["direction"] == "out" else (m["peer"], s["name"])
            log.append({"at": m["at"], "from": frm, "to": to, "summary": m["summary"]})

    log = sorted((m for m in log if m["at"]), key=lambda m: m["at"])[-MESSAGE_LIMIT:]
    for m in log:
        m["at"] = _iso(m["at"])
    out.sort(key=lambda x: (x["floor"], x["name"]))
    floor_list = [BOSS_FLOOR] + [{k: fl[k] for k in ("id", "name", "order")}
                                 for fl in sorted(floors, key=lambda f: -f["order"])]
    return {"title": cfg["title"], "version": VERSION, "observedAt": _iso(now), "floors": floor_list, "sessions": out, "messages": log,
            "cats": cfg.get("cats") or {},  # 캐릭터 → 고양이 털 종류(화면이 그림·성격을 고른다). 없으면 화면이 이름 해시로
            "weather": cfg.get("weather")}  # {lat, lon} 이면 화면이 open-meteo 에 날씨를 묻는다. 없으면 null — 외부 요청 없음
