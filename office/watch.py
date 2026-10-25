"""기다림 감시 — 10초마다 상태를 읽어 요약 파일(입력창 띠가 읽는다)을 쓰고, 새 기다림이 생기면 맥 알림을 띄운다."""
import json
import shutil
import os
import subprocess
from datetime import datetime
from pathlib import Path

from office import config
from office.state import build_state

SUMMARY = config.cache_dir() / "wait.json"  # 밖(입력창 띠 등)이 읽는 경로 — 바꾸면 그쪽도 같이. 옛 Knock Office 캐시 폴더가 있으면 그대로 그곳
EVERY = 10          # 초
NOTIFY_GAP = 600    # 알림 사이 최소 10분 — 그동안 새로 생긴 기다림은 다음 알림 하나로 묶는다
TEXT = {"ko": ("기다림 {n}", " 외 {more}명"), "en": ("Waiting: {n}", " +{more} more")}  # 알림 제목·「외 N명」


def summarize(st):
    w = sorted((s for s in st["sessions"] if s["state"] == "waiting"), key=lambda s: s["waitingSince"] or "9")
    return {
        "observedAt": st["observedAt"],
        "count": len(w),
        "oldest": {"name": w[0]["name"], "since": w[0]["waitingSince"]} if w else None,
        "waiting": [{"id": s["id"], "name": s["name"], "text": s["lastText"]} for s in w],
    }


class Watcher:
    def __init__(self, known_ids, lang="ko"):
        self.text = TEXT.get(lang, TEXT["ko"])
        self.known = set(known_ids)   # 이미 알린(또는 서버 시작 때부터 있던) 기다림
        self.pending = []             # 아직 안 알린 새 기다림
        self.last_sent = 0.0

    def tick(self, summary, now):
        """알릴 게 있으면 (제목, 내용, 세션 id) 를 돌려준다."""
        waiting = {w["id"]: w for w in summary["waiting"]}
        self.pending = [w for w in self.pending if w["id"] in waiting]  # 그사이 답한 건 뺀다
        for i, w in waiting.items():
            if i not in self.known:
                self.known.add(i)
                self.pending.append(w)
        self.known &= set(waiting)  # 답했다가 다시 기다리면 새 기다림으로 본다
        if not self.pending or now - self.last_sent < NOTIFY_GAP:
            return None
        first, more = self.pending[0], len(self.pending) - 1
        self.pending, self.last_sent = [], now
        who = first["name"] + (self.text[1].format(more=more) if more else "")
        return self.text[0].format(n=summary["count"]), f"{who}: {(first['text'] or '').strip()[:80]}", first["id"]


def notify(title, message, sid, port, app="Kitty Office"):
    if os.name == "nt":
        return  # 맥 알림 도구가 없다 — 브라우저 탭 제목·소리로 대신
    tn = shutil.which("terminal-notifier") or "/opt/homebrew/bin/terminal-notifier"
    url = f"http://127.0.0.1:{port}/#s={sid}"
    try:
        subprocess.run([tn, "-title", app, "-subtitle", title, "-message", message,
                        "-open", url, "-group", "kitty-office"], timeout=10, capture_output=True)
    except OSError:  # terminal-notifier 가 없으면 클릭 이동 없는 기본 알림. AppleScript 는 \uXXXX 를 몰라 한글을 그대로 넣는다
        q = lambda t: json.dumps(t, ensure_ascii=False)
        subprocess.run(["osascript", "-e", f"display notification {q(message)} with title {q(title)}"],
                       timeout=10, capture_output=True)


def write_summary(summary):
    SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    tmp = SUMMARY.with_suffix(".tmp")
    tmp.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SUMMARY)  # 띠가 반쯤 쓴 파일을 읽지 않게


def run(stop, port, cfg):
    try:  # 서버를 다시 띄워도 이미 있던 기다림을 또 알리지 않게
        known = [w["id"] for w in json.loads(SUMMARY.read_text(encoding="utf-8"))["waiting"]]
    except (OSError, ValueError, KeyError, TypeError):
        known = None
    watcher = None
    while not stop.is_set():
        try:
            now = datetime.now().astimezone()
            summary = summarize(build_state(Path.home(), now, cfg))
            write_summary(summary)
            if watcher is None:  # 첫 바퀴는 지금 있는 기다림을 다 아는 것으로 친다
                watcher = Watcher(known if known is not None else [w["id"] for w in summary["waiting"]], config.notify_lang(cfg))
            note = watcher.tick(summary, now.timestamp())
            if note and cfg["notify"]:
                notify(*note, port, cfg["title"])
        except Exception as e:  # 감시가 죽어도 화면 서버는 계속 산다
            print(f"감시 오류: {type(e).__name__}: {e}")
        stop.wait(EVERY)
