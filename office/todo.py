"""오늘 할 일 칠판 — 설정 폴더의 todo.json 하나. 화면에서 적고 체크한다(파일을 열 일이 없게).
모양: {"items": [{"id", "text", "done", "created", "doneAt"}]}. 시각은 로컬 ISO 문자열.
읽을 때 정리: 끝낸 지 30일 넘은 기록만 지운다(끝낸 기록 최대 500개). 못 한 항목은 남긴다."""
import json
import secrets
import threading
from datetime import datetime
from pathlib import Path

from office import config

TEXT_MAX = 200
ITEMS_MAX = 100  # 안 끝난 항목만 센다
DONE_KEEP_DAYS = 30  # 끝낸 기록은 이만큼 보관 — 오늘 끝낸 건 목록에, 그 전은 「끝낸 일」로 접어 보인다
DONE_MAX = 500  # 끝낸 기록 상한 — 넘으면 오래 전에 끝낸 것부터 지운다
OLD_DAYS = 3  # created 가 이만큼 지났는데 안 끝났으면 화면이 빨갛게
LATE_DAYS = 7  # 이만큼 지났는데 안 끝났으면 「밀린 일」로 접어 둔다(지우지 않는다). 칠판·세기에서 뺀다
LOCK = threading.Lock()  # ponytail: 서버 하나 안의 잠금 — 서버를 둘 띄우면 같은 파일을 서로 덮어쓸 수 있다


class TodoError(ValueError):
    """화면에 보일 이유 코드(server.MSG 의 키)를 담는다."""


def path():
    return config.path().parent / "todo.json"


def _now():
    return datetime.now().astimezone()


def _read(p):
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        items = data.get("items") if isinstance(data, dict) else None
        return [i for i in items if isinstance(i, dict) and isinstance(i.get("id"), str)] if isinstance(items, list) else []
    except (FileNotFoundError, ValueError):
        return []


def _write(p, items):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _done_day(i):
    try:
        return datetime.fromisoformat(i["doneAt"]).date()
    except (KeyError, TypeError, ValueError):
        return None


def _tidy(items, now):
    """끝낸 지 30일 넘은 기록을 지우고, 끝낸 기록이 500개를 넘으면 오래 전에 끝낸 것부터 지운다. 안 끝난 건 그대로."""
    today = now.date()
    keep = [i for i in items if not (i.get("done") and (today - (_done_day(i) or today)).days > DONE_KEEP_DAYS)]
    done = sorted((i for i in keep if i.get("done")), key=lambda i: str(i.get("doneAt") or ""), reverse=True)
    drop = {id(i) for i in done[DONE_MAX:]}
    return [i for i in keep if id(i) not in drop]


def _view(items, now):
    out = []
    for i in items:
        try:
            age = (now - datetime.fromisoformat(i["created"])).days
        except (KeyError, ValueError, TypeError):
            age = 0
        dd = _done_day(i) if i.get("done") else None
        out.append({**i, "old": not i.get("done") and age >= OLD_DAYS, "late": not i.get("done") and age >= LATE_DAYS,
                    "past": bool(dd and dd < now.date())})  # 어제 이전에 끝낸 기록 — 화면이 「끝낸 일」로 접는다
    return out


def load(p=None, now=None):
    """정리한 목록(+ 화면용 old 표시). 정리로 바뀌었으면 파일에도 쓴다."""
    p, now = Path(p) if p else path(), now or _now()
    with LOCK:
        items = _read(p)
        kept = _tidy(items, now)
        if len(kept) != len(items):
            _write(p, kept)
    return _view(kept, now)


def summary(p=None, now=None):
    """칠판·목록 줄용 — 오늘 기준. 밀린 일(late)·지난 끝낸 기록(past)은 done·total 에서 빼고 따로 센다."""
    items = load(p, now)
    cur = [i for i in items if not i["late"] and not i["past"]]
    return {"done": sum(1 for i in cur if i.get("done")), "total": len(cur),
            "late": sum(1 for i in items if i["late"]), "past": sum(1 for i in items if i["past"])}


def _text(v):
    t = " ".join(str(v or "").split())
    if not t:
        raise TodoError("todoEmpty")
    if len(t) > TEXT_MAX:
        raise TodoError("todoLong")
    return t


def act(body, p=None, now=None):
    """화면에서 온 바꾸기 하나 — op: add(text) · toggle(id) · edit(id, text) · delete(id) · today(id: 작성일을 지금으로 — 밀린 일을 다시 위로) · order(ids: 끌어서 바꾼 순서). 바뀐 목록을 돌려준다."""
    p, now = Path(p) if p else path(), now or _now()
    op = body.get("op")
    with LOCK:
        items = _tidy(_read(p), now)
        if op == "add":
            if sum(1 for i in items if not i.get("done")) >= ITEMS_MAX:
                raise TodoError("todoFull")
            items.append({"id": secrets.token_hex(6), "text": _text(body.get("text")), "done": False,
                          "created": now.isoformat(timespec="seconds"), "doneAt": None})
        elif op == "order":  # 끌어서 바꾼 순서 — 받은 id 들이 있던 자리에 그 순서대로 다시 앉힌다(모르는 id 는 무시)
            ids = body.get("ids") if isinstance(body.get("ids"), list) else []
            pos = {x: n for n, x in enumerate(ids) if isinstance(x, str)}
            moved = iter(sorted((i for i in items if i["id"] in pos), key=lambda i: pos[i["id"]]))
            items = [next(moved) if i["id"] in pos else i for i in items]
        else:
            it = next((i for i in items if i["id"] == body.get("id")), None)
            if op not in ("toggle", "edit", "delete", "today"):
                raise TodoError("todoOp")
            if it is None:
                raise TodoError("todoGone")
            if op == "toggle":
                it["done"] = not it.get("done")
                it["doneAt"] = now.isoformat(timespec="seconds") if it["done"] else None
            elif op == "today":
                it["created"] = now.isoformat(timespec="seconds")
                items.remove(it); items.append(it)  # 오늘 적은 것처럼 맨 아래(새 항목 자리)로
            elif op == "edit":
                it["text"] = _text(body.get("text"))
            else:
                items.remove(it)
        _write(p, items)
    return _view(items, now)
