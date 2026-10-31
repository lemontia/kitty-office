"""Kitty Office 서버 — /api/state 는 상태 JSON, 그 밖은 web/ 정적 파일. 127.0.0.1 에만 연다.
원격(tailscale serve 등)으로 열 땐 설정의 allowedHosts 에 그 Host 를 적고 token 으로 잠근다(token 없이 allowedHosts 만 있으면 띄우지 않는다)."""
import base64
import hmac
import json
import os
import re
import secrets
import socketserver
import threading
import time
from datetime import date, datetime
from functools import partial
from http.cookies import CookieError, SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from office import config, demo, term, todo, watch
from office.state import build_state, folders, load_sessions

PORT = int(os.environ.get("OFFICE_PORT", "5430"))
DEMO = os.environ.get("KITTY_OFFICE_DEMO") == "1"  # 가짜 사무실(office/demo.py) — 실제 세션·설정·터미널을 건드리지 않는다
WEB = Path(__file__).resolve().parent.parent / "web"


UPLOAD_DIR = config.cache_dir() / "uploads"
UPLOAD_MAX = 20 * 1024 * 1024
COOKIE = "knock_token"
# 사람에게 보이는 문구 — 한국어·영어. 화면이 X-Lang 으로 알려 주고, 없으면 ?lang= · Accept-Language(ko 로 시작하면 ko)
MSG = {
    "locked": ("잠겨 있습니다 — 첫 화면에서 키를 넣거나, 주소 끝에 ?token=<설정의 token> 을 붙여 여세요.\n",
               "Locked — enter the key on the first page, or add ?token=<token from config> to the address.\n"),
    "badHost": ("허용하지 않은 Host 입니다 — 설정의 allowedHosts 에 적으세요.\n", "Host not allowed — add it to allowedHosts in the config.\n"),
    "tooMany": ("키를 너무 많이 틀렸습니다 — {n}분 뒤에 다시 하세요", "Too many wrong keys — try again in {n} min"),
    "badKey": ("키가 맞지 않습니다", "Wrong key"),
    "noPath": ("없는 주소", "Not found"),
    "origin": ("이 화면에서 온 요청만 받는다", "Only requests from this page are accepted"),
    "tooBig": ("파일이 너무 크다(20MB 까지)", "File too large (20 MB max)"),
    "folders": ("폴더 목록이 필요하다", "A folder list is required"),
    "islandName": ("이름은 30자까지, 줄바꿈 없이", "Name: up to 30 characters, no line breaks"),
    "noTab": ("cmux·Orca·herdr 밖에서 띄운 세션이다", "This session was started outside cmux, Orca or herdr"),
    "dialog": ("승인·선택 창이 떠 있다 — 탭으로 가서 고른다", "An approval prompt is open — answer it in the tab"),
    "noDialog": ("떠 있는 승인·선택 창이 없습니다", "No approval or choice prompt is open"),
    "dialogChanged": ("선택 창이 그새 바뀌었습니다 — 다시 보고 골라 주세요", "The prompt changed — look again and pick"),
    "dialogMove": ("그 칸으로 옮기지 못했습니다 — 터미널에서 골라 주세요", "Couldn't move to that option — pick it in the terminal"),
    "busy": ("일하는 중에는 끝낼 수 없습니다 — 먼저 「하던 일 중단하기」", "Can't end while working — press Stop first"),
    "renameRule": ("이름은 1~40자, 줄바꿈·/ 없이", "Name: 1–40 characters, no line breaks or /"),
    "empty": ("빈 답", "Empty reply"),
    "todoEmpty": ("할 일을 적어 주세요", "Write something first"),
    "todoLong": ("할 일은 200자까지", "Up to 200 characters"),
    "todoFull": ("할 일은 100개까지", "Up to 100 items"),
    "todoGone": ("이미 없는 항목이다", "That item is gone"),
    "todoOp": ("모르는 요청", "Unknown request"),
    "stillRunning": ("세션이 아직 안 끝났습니다 — 탭은 직접 닫으세요", "The session hasn't ended yet — close the tab yourself"),
    "closeFailed": ("세션은 끝났지만 탭을 닫지 못했습니다({e}) — 탭은 직접 닫으세요", "The session ended but its tab didn't close ({e}) — close it yourself"),
}
_LOGIN = """<!doctype html><html lang={lang}><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Kitty Office</title><script>if(!location.pathname.endsWith('/')&&!/\\.[a-z0-9]+$/i.test(location.pathname))location.replace(location.pathname+'/'+location.search+location.hash)</script><style>body{{font-family:system-ui,"Apple SD Gothic Neo",sans-serif;background:#f6ecd9;display:grid;place-items:center;
min-height:100vh;margin:0}}form{{background:#fff;padding:28px;border-radius:16px;box-shadow:0 10px 30px #5a3d2833;width:min(320px,86vw)}}
h1{{margin:0 0 6px;font-size:20px}}p{{margin:0 0 14px;color:#8a7a6a;font-size:13px}}input{{width:100%;box-sizing:border-box;font:inherit;
padding:10px;border:1px solid #d9c6a8;border-radius:10px}}button{{margin-top:10px;width:100%;padding:10px;border:0;border-radius:10px;
background:#5a3d28;color:#fff3e0;font:inherit;font-weight:700}}#e{{color:#c0392b;font-size:13px;min-height:1em;margin-top:8px}}</style>
<form id=f><h1>Kitty Office</h1><p>{intro}</p>
<input id=k type=password autocomplete=current-password autofocus placeholder="{key}"><button>{open}</button><div id=e></div></form>
<script>f.onsubmit=async(ev)=>{{ev.preventDefault();const r=await fetch('api/login',{{method:'POST',headers:{{'Content-Type':'application/json','X-Lang':'{lang}'}},
body:JSON.stringify({{token:k.value}})}});if(r.ok)location.replace(location.pathname+location.hash);else{{e.textContent=(await r.json().catch(()=>({{}}))).error||'{bad}';k.select();}}}}</script>"""
LOGIN = {
    "ko": _LOGIN.format(lang="ko", intro="이 화면은 키를 아는 사람만 엽니다. 설정 파일의 token 을 넣으세요.", key="키", open="열기", bad="키가 맞지 않습니다").encode(),
    "en": _LOGIN.format(lang="en", intro="Only people with the key can open this page. Enter the token from the config file.", key="Key", open="Open", bad="Wrong key").encode(),
}
# 토큰으로 처음 열면 쿠키를 심고 토큰 없는 주소로 넘긴다. 303 대신 페이지 안에서 넘기는 건,
# 다른 앱의 링크로 열었을 때 SameSite=Strict 쿠키가 리다이렉트 다음 요청에 안 실리기 때문이다
ENTER = b"<!doctype html><meta charset=utf-8><script>location.replace(location.pathname + location.hash)</script>"


def save_upload(name, data):
    """data 는 base64(또는 data: URL). 파일 이름은 시각 + 원래 확장자만 쓴다(경로 조작 막기)."""
    raw = base64.b64decode(str(data or "").split(",", 1)[-1], validate=True)
    if not raw or len(raw) > UPLOAD_MAX:
        raise ValueError("빈 파일이거나 20MB 를 넘는다")
    ext = re.sub(r"[^a-z0-9]", "", Path(str(name or "")).suffix.lower())[:8] or "png"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    out = UPLOAD_DIR / f"{datetime.now():%m%d-%H%M%S}-{secrets.token_hex(3)}.{ext}"
    out.write_bytes(raw)
    return str(out)


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        # 화면 파일을 브라우저가 쌓아 두고 옛 것을 쓰지 않게 — 매번 새로 확인한다(바뀌지 않았으면 304 로 가볍다)
        if not any(h.lower().startswith(b"cache-control") for h in getattr(self, "_headers_buffer", [])):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, *args):  # 요청 로그는 끈다(오류는 log_error 가 따로 stderr 로)
        pass

    def _hosts(self):
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}", *self.server.cfg["allowedHosts"]}

    def _blocked(self):
        """모든 요청 앞에서 본다. 막았으면(응답을 이미 보냈으면) True."""
        if self.headers.get("Host") not in self._hosts():  # 다른 이름으로 들어온 요청(DNS 리바인딩 등)
            self._text(403, self._m("badHost"))
            return True
        token = str(self.server.cfg.get("token") or "").encode()
        # token 이 없으면 로컬 전용이다(allowedHosts 를 적었는데 token 이 없으면 config.load 가 기동을 막는다).
        # token 이 있으면 127.0.0.1 도 키를 묻는다 — Host 를 127.0.0.1 로 바꿔 넘기는 프록시 뒤에서도 잠기게
        if not token:
            return False
        if self.command == "POST" and urlsplit(self.path).path == "/api/login":
            return self._login(token)
        url = urlsplit(self.path)
        given = parse_qs(url.query).get("token", [""])[0].encode() if self.command == "GET" else b""
        header = (self.headers.get("X-Token") or "").encode()
        if given or header:  # 키를 직접 넣은 시도 — 로그인과 같은 틀린 횟수 잠금을 거친다
            who = self._who()
            wait = LOCKOUT.blocked(who, time.time())
            if wait:
                self._text(429, self._m("tooMany", n=wait // 60 + 1) + "\n")
                return True
            if given and hmac.compare_digest(given, token):
                self.send_response(200)
                self.send_header("Set-Cookie", f"{COOKIE}={token.decode()}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000")
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(ENTER)))
                self.end_headers()
                self.wfile.write(ENTER)
                return True
            if header and hmac.compare_digest(header, token):
                return False
            LOCKOUT.fail(who, time.time())
            time.sleep(1)
        try:
            jar = SimpleCookie(self.headers.get("Cookie") or "")
        except CookieError:
            jar = {}
        got = (jar[COOKIE].value if COOKIE in jar else "").encode()
        if hmac.compare_digest(got, token):
            return False
        if self.command == "GET" and urlsplit(self.path).path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", LOGIN[self._lang()])
        else:
            self._text(401, self._m("locked"))
        return True

    def _lang(self):
        q = parse_qs(urlsplit(self.path).query).get("lang", [""])[0]
        lang = self.headers.get("X-Lang") or q
        if lang in ("ko", "en"):
            return lang
        return "ko" if (self.headers.get("Accept-Language") or "ko").strip().lower().startswith("ko") else "en"

    def _m(self, key, **kw):
        return MSG[key][self._lang() == "en"].format(**kw)

    def _who(self):
        # 마지막 값이 프록시(Tailscale)가 붙인 실제 접속 주소 — 앞쪽은 접속자가 마음대로 적을 수 있다
        return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[-1].strip()

    def _login(self, token):
        """키 입력 화면에서 온 POST. 맞으면 쿠키를 심는다. 틀리면 1초 늦게 답하고, 많이 틀리면 막는다(Lockout)."""
        who = self._who()
        wait = LOCKOUT.blocked(who, time.time())
        if wait:
            return self._json(429, {"error": self._m("tooMany", n=wait // 60 + 1)}) or True
        ok = {f"https://{h}" for h in self._hosts()} | {f"http://{h}" for h in self._hosts()}
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 10_000)
            given = str(json.loads(self.rfile.read(n) or b"{}").get("token") or "").encode()
        except (ValueError, AttributeError):
            given = b""
        if self.headers.get("Origin") in ok and given and hmac.compare_digest(given, token):
            secure = "; Secure" if (self.headers.get("X-Forwarded-Proto") or "") == "https" or self.headers.get("Origin", "").startswith("https") else ""
            self.send_response(200)
            self.send_header("Set-Cookie", f"{COOKIE}={token.decode()}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000{secure}")
            body = b'{"ok": true}'
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True
        LOCKOUT.fail(who, time.time())
        time.sleep(1)
        self._json(401, {"error": self._m("badKey")})
        return True

    def _send(self, code, ctype, data):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self):
        if not self._blocked():
            super().do_HEAD()

    def do_GET(self):
        if self._blocked():
            return
        if self.path.split("?")[0] == "/api/todo":  # 오늘 할 일 칠판
            return self._json(200, {"items": todo.load(demo.TODO_PATH) if DEMO else todo.load()})
        path = self.path.split("?")[0]
        if DEMO and path in ("/", "/index.html"):  # 데모 표시 — 화면이 고정 씨앗으로 무작위를 돌린다(녹화를 다시 해도 같은 화면). 열 때마다 처음 상태로
            demo.reset()
            data = (WEB / "index.html").read_text(encoding="utf-8").replace("<head>", "<head>\n<script>window.KITTY_DEMO = 1;</script>", 1).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path.startswith("/sounds/") and ".." not in path and not (WEB / path.lstrip("/")).is_file():  # 아직 안 넣은 소리 파일 — 404 대신 빈 204(브라우저 콘솔에 빨간 줄이 안 남고, 화면은 소리 없이 넘어간다)
            self.send_response(204)
            self.end_headers()
            return
        if path != "/api/state":
            return super().do_GET()
        try:
            if DEMO:
                return self._json(200, demo.state())
            home, cfg = Path.home(), self.server.cfg
            fds = folders(home)
            # 처음 본 폴더가 있거나 오늘 아직 「봤다」고 안 적은 폴더가 있을 때만 — 설정 파일 쓰기는 하루 몇 번으로 그친다
            day = date.today().isoformat()
            if self.server.seen_day[0] != day:
                self.server.seen_day = (day, set())
            if fds - cfg.get("colors", {}).keys() or fds - self.server.seen_day[1]:
                cfg["colors"] = config.assign_colors(fds)
                self.server.seen_day[1].update(fds)
            code, body = 200, build_state(home, datetime.now().astimezone(), cfg)
            body["todo"] = todo.summary()  # 칠판·목록 줄용 요약만 — 항목은 /api/todo
        except Exception as e:  # 한 번 실패해도 서버는 계속 산다
            code, body = 500, {"error": f"{type(e).__name__}: {e}"}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


    # 팝업에서 「탭으로 가기」·「답 보내기」 — cmux·Orca·herdr 에서 띄운 세션만
    def do_POST(self):
        if self._blocked():
            return
        path = self.path.split("?")[0]
        if path not in ("/api/focus", "/api/answer", "/api/rename", "/api/upload", "/api/island", "/api/stop", "/api/exit", "/api/todo", "/api/clippaths", "/api/dialog", "/api/pick"):
            return self._json(404, {"error": self._m("noPath")})
        # 다른 웹페이지가 브라우저를 통해 127.0.0.1 로 보내는 요청을 막는다(이 화면에서 온 것만)
        ok = {f"{scheme}://{h}" for h in self._hosts() for scheme in ("http", "https")}
        if self.headers.get("Origin") not in ok or not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._json(403, {"error": self._m("origin")})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > UPLOAD_MAX * 4 // 3 + 10_000:
                return self._json(413, {"error": self._m("tooBig")})
            body = json.loads(self.rfile.read(n) or b"{}")
            if DEMO:  # 데모 — 할 일은 임시 폴더, 답은 화면 안에서만. 터미널·설정 파일엔 아무것도 안 한다
                if path == "/api/todo":
                    try:
                        return self._json(200, {"items": todo.act(body, demo.TODO_PATH)})
                    except todo.TodoError as e:
                        return self._json(400, {"error": self._m(str(e))})
                if path == "/api/answer":
                    return self._json(200, demo.answer(body.get("id"), body.get("text")))
                return self._json(200, {"ok": True, "path": "demo-file", "paths": []})
            if path == "/api/upload":  # 팝업에 붙인 이미지·파일을 저장하고 경로를 돌려준다 — 답에 경로로 넣는다
                try:
                    return self._json(200, {"path": save_upload(body.get("name"), body.get("data"))})
                except ValueError:  # 빈 파일이거나 너무 크다
                    return self._json(413, {"error": self._m("tooBig")})
            if path == "/api/clippaths":  # 붙여넣은 파일의 원래 경로 — 서버 컴퓨터의 클립보드라 이 컴퓨터에서 연 화면에만 준다
                local = self._who() in ("127.0.0.1", "::1") and not self.headers.get("X-Forwarded-For")
                return self._json(200, {"paths": term.clipboard_paths() if local else []})
            if path == "/api/todo":  # 할 일 추가·체크·고치기·지우기 — 설정 폴더 todo.json
                try:
                    return self._json(200, {"items": todo.act(body)})
                except todo.TodoError as e:
                    return self._json(400, {"error": self._m(str(e))})
            if path == "/api/island":  # 섬 이름 바꾸기·합치기 — 설정 파일 islands 에 쓰고 바로 반영
                folders, name = body.get("folders"), str(body.get("name") or "").strip()
                if (not isinstance(folders, list) or not folders or len(folders) > 50
                        or not all(isinstance(f, str) and 0 < len(f) <= 200 for f in folders)):
                    return self._json(400, {"error": self._m("folders")})
                if len(name) > 30 or any(c in name for c in "\r\n"):
                    return self._json(400, {"error": self._m("islandName")})
                self.server.cfg["islands"] = config.save_islands({f: name for f in folders})
                return self._json(200, {"ok": True})
            s = next((x for x in load_sessions(Path.home()) if x.get("sessionId") == body.get("id")), None)
            tab = s and term.tab_of(s.get("pid"))
            if not tab:
                return self._json(409, {"error": self._m("noTab")})
            if path == "/api/focus":
                term.focus(tab)
                return self._json(200, {"ok": True})
            if path in ("/api/dialog", "/api/pick") and s.get("status") != "waiting":
                return self._json(200 if path == "/api/dialog" else 409, {"dialog": None, "error": self._m("noDialog")})
            if path == "/api/dialog":  # 떠 있는 승인·선택 창의 질문·선택지 — 터미널 화면에서 읽는다(herdr·cmux)
                return self._json(200, {"dialog": term.dialog(tab)})
            if path == "/api/pick":  # 팝업에서 선택지를 누름 — 화면이 그대로일 때만 그 칸을 고른다
                try:
                    term.pick(tab, int(body.get("n")), str(body.get("label") or ""))
                except (TypeError, ValueError):
                    return self._json(400, {"error": self._m("noDialog")})
                except RuntimeError as e:
                    return self._json(409, {"error": self._m(str(e)) if str(e) in MSG else str(e)})
                return self._json(200, {"ok": True})
            if path == "/api/stop":  # 일하는 중인 세션의 지금 차례를 멈춘다(Esc)
                term.stop(tab)
                return self._json(200, {"ok": True})
            if path == "/api/exit":  # 세션 끝내기 — /exit 후 그 탭만 닫는다. 화면이 막아도 여기서 한 번 더 막는다
                if s.get("status") == "waiting":
                    return self._json(409, {"error": self._m("dialog")})
                st = next((x for x in build_state(Path.home(), datetime.now().astimezone(), self.server.cfg)["sessions"]
                           if x.get("id") == s.get("sessionId")), None)
                if st and st.get("state") in ("working", "meeting"):
                    return self._json(409, {"error": self._m("busy")})
                note = term.exit_session(tab, s.get("pid"))  # None · "still_running" · ("close_failed", 오류)
                if note == "still_running":
                    note = self._m("stillRunning")
                elif note:
                    note = self._m("closeFailed", e=note[1])
                return self._json(200, {"ok": True, "note": note})
            if path == "/api/rename":  # 세션 입력창에 /rename 을 쳐서 Claude Code 의 진짜 세션 이름을 바꾼다
                name = str(body.get("name") or "").strip()
                if not name or len(name) > 40 or any(c in name for c in "\r\n/"):
                    return self._json(400, {"error": self._m("renameRule")})
                body["text"] = "/rename " + name
            text = str(body.get("text") or "").strip()
            if not text:
                return self._json(400, {"error": self._m("empty")})
            if s.get("status") == "waiting":  # 승인·선택 창이 떠 있으면 글자가 엉뚱한 선택지를 누를 수 있다
                return self._json(409, {"error": self._m("dialog")})
            term.answer(tab, text)
            return self._json(200, {"ok": True})
        except Exception as e:
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def _text(self, code, msg):
        data = msg.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Lockout:
    """틀린 키 막기 — 같은 IP 가 PER_IP 번 틀리면 IP_BLOCK 초, 전체가 한 시간에 TOTAL 번 넘게 틀리면 모든 원격 키 입력을 ALL_BLOCK 초.
    ponytail: 메모리에만 둔다 — 서버를 다시 띄우면 풀린다. 오래 막아야 하면 파일로"""
    PER_IP, IP_WINDOW, IP_BLOCK = 5, 15 * 60, 30 * 60
    TOTAL, ALL_WINDOW, ALL_BLOCK = 30, 60 * 60, 60 * 60

    def __init__(self):
        self.fails, self.until, self.all_fails, self.all_until = {}, {}, [], 0.0
        self.lock = threading.Lock()

    def blocked(self, who, now):
        """막혀 있으면 남은 초, 아니면 0."""
        with self.lock:
            return int(max(self.until.get(who, 0), self.all_until) - now) if max(self.until.get(who, 0), self.all_until) > now else 0

    def fail(self, who, now):
        with self.lock:
            f = [t for t in self.fails.get(who, []) if t > now - self.IP_WINDOW] + [now]
            self.fails[who] = f
            if len(f) >= self.PER_IP:
                self.until[who], self.fails[who] = now + self.IP_BLOCK, []
            self.all_fails = [t for t in self.all_fails if t > now - self.ALL_WINDOW] + [now]
            if len(self.all_fails) > self.TOTAL:
                self.all_until, self.all_fails = now + self.ALL_BLOCK, []


LOCKOUT = Lockout()


class Server(ThreadingHTTPServer):
    def server_bind(self):
        # 기본 server_bind 는 socket.getfqdn 으로 자기 이름을 조회하는데, 이 조회가 끝나지 않는 맥이 있다
        # (2026-10-21 m1server 에서 기동이 멈춤). 쓰지 않는 값이라 건너뛴다
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def make_server(cfg, port=PORT):
    server = Server(("127.0.0.1", port), partial(Handler, directory=str(WEB)))
    server.cfg = cfg
    server.seen_day = ("", set())  # 오늘 colors_seen 에 적은 폴더(하루에 한 번만 쓰게)
    return server


def main():
    try:
        cfg = {**config.DEFAULT} if DEMO else config.load()  # 데모는 설정 파일을 안 읽는다(우리 이름·고양이 배정이 안 새게)
    except ValueError as e:  # 설정이 깨졌거나 잠금 없이 원격에 열려는 설정 — 띄우지 않는다
        raise SystemExit(f"띄우지 않음: {e}")
    server = make_server(cfg)
    stop = threading.Event()
    if DEMO:
        demo.seed_todo()
        print(f"{cfg['title']} 데모: http://127.0.0.1:{PORT}  (가짜 사무실 — 실제 세션·설정을 안 읽는다)", flush=True)
    else:
        print(f"{cfg['title']}: http://127.0.0.1:{PORT}  (설정 {config.path()})", flush=True)
        threading.Thread(target=watch.run, args=(stop, PORT, cfg), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()


if __name__ == "__main__":
    main()
