"""설정 파일 읽기와 원격 접속 잠금 테스트 — 서버는 임시 포트로 띄웠다 끈다."""
import http.client
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from office import config, server
from office.state import build_state

NOW = datetime(2026, 10, 21, 1, 0, 0, tzinfo=timezone.utc)


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def session(self, home, name, cwd):
        (home / ".claude/sessions").mkdir(parents=True, exist_ok=True)
        (home / f".claude/sessions/{name}.json").write_text(json.dumps(
            {"pid": os.getpid(), "sessionId": name, "cwd": cwd, "name": name, "status": "idle", "entrypoint": "cli"}))

    def test_missing_file_gives_defaults(self):
        cfg = config.load(self.dir / "없음.json")
        self.assertEqual(cfg["title"], "Kitty Office")
        self.assertEqual([f["id"] for f in cfg["floors"]], ["lobby"])
        self.assertTrue(cfg["notify"])
        self.assertIsNone(cfg["token"])
        home = self.dir / "home"
        self.session(home, "app-1a", str(home / "work/app"))
        st = build_state(home, NOW, cfg)
        self.assertEqual([f["id"] for f in st["floors"]], ["boss", "lobby"])
        s = st["sessions"][0]
        self.assertEqual(s["floor"], "lobby")
        self.assertIn(s["character"], {"g1", "g2", "g3", "g4"})
        self.assertEqual(st["title"], "Kitty Office")

    def test_file_sets_floors_seats_names(self):
        p = self.dir / "c.json"
        p.write_text(json.dumps({
            "title": "우리 사무실",
            "floors": [{"id": "client", "name": "개발", "order": 0, "prefixes": ["~/work/app"]}],
            "seats": {"~/work/blog": "carol"},
            "names": {"글쓰기": "carol"},
            "notify": False,
        }, ensure_ascii=False))
        with mock.patch.dict(os.environ, {"KITTY_OFFICE_CONFIG": str(p)}):
            cfg = config.load()
        self.assertFalse(cfg["notify"])
        self.assertEqual([f["id"] for f in cfg["floors"]], ["client", "lobby"])  # 로비는 빠져 있으면 붙인다
        home = self.dir / "home"
        self.session(home, "app-1a", str(home / "work/app/src"))
        self.session(home, "blog-2b", str(home / "work/blog"))
        st = build_state(home, NOW, cfg)
        by = {s["name"]: s for s in st["sessions"]}
        self.assertEqual(st["title"], "우리 사무실")
        self.assertEqual(by["app-1a"]["floor"], "client")
        self.assertEqual(by["글쓰기-1"]["character"], "carol")  # 자리 폴더의 자동 이름 → 표시 이름-번호
        self.assertEqual(by["글쓰기-1"]["floor"], "lobby")

    def test_broken_file_is_an_error(self):
        p = self.dir / "c.json"
        p.write_text("{ 깨짐")
        with self.assertRaises(ValueError):
            config.load(p)

    def test_remote_without_token_refuses_to_start(self):
        """원격 이름을 적었는데 키가 없거나 짧으면 기동 거부 — 그대로 띄우면 남이 터미널에 글을 넣는다."""
        p = self.dir / "c.json"
        for bad in ({"allowedHosts": ["myhost.example.com:8444"]}, {"allowedHosts": ["myhost.example.com:8444"], "token": ""},
                    {"token": "short-key"}):
            p.write_text(json.dumps(bad))
            with self.assertRaises(ValueError, msg=bad):
                config.load(p)
        p.write_text(json.dumps({"allowedHosts": ["myhost.example.com:8444"], "token": "x" * 16}))
        self.assertEqual(config.load(p)["token"], "x" * 16)


    def test_save_islands_keeps_other_keys_and_names_island(self):
        p = self.dir / "c.json"
        p.write_text(json.dumps({"title": "우리", "islands": {"old": "옛"}}, ensure_ascii=False))
        self.assertEqual(config.save_islands({"blog": "글쓰기", "app": "글쓰기", "old": ""}, p), {"blog": "글쓰기", "app": "글쓰기"})
        cfg = config.load(p)
        self.assertEqual(cfg["title"], "우리")
        home = self.dir / "home"
        self.session(home, "b-1", str(home / "work/blog"))
        self.session(home, "c-1", str(home / "work/etc"))
        isl = {s["name"]: s["island"] for s in build_state(home, NOW, cfg)["sessions"]}
        self.assertEqual(isl, {"b-1": "글쓰기", "c-1": "etc"})

    def test_colors_stay_and_differ(self):
        p = self.dir / "c.json"
        p.write_text(json.dumps({"title": "우리"}, ensure_ascii=False))
        first = config.assign_colors({"blog", "app", "shop", "docs"}, p)
        self.assertEqual(len(set(first.values())), 4)                       # 지금 보이는 폴더끼리 안 겹친다
        mtime = p.stat().st_mtime_ns
        self.assertEqual(config.assign_colors({"blog", "app"}, p), first)   # 같은 폴더는 같은 색, 새 게 없으면
        self.assertEqual(p.stat().st_mtime_ns, mtime)                       # 파일을 안 쓴다
        self.assertEqual(config.load(p)["colors"], first)                   # 다시 읽어도 같다
        self.assertEqual(config.load(p)["title"], "우리")                    # 다른 칸은 그대로
        more = config.assign_colors({"blog", "app", "a1", "a2", "a3"}, p)
        self.assertEqual({k: more[k] for k in first}, first)                # 옛 폴더 색은 안 바뀐다
        self.assertEqual(len({more[k] for k in ("blog", "app", "a1", "a2", "a3")}), 5)
        self.assertNotIn("#d9472b", more.values())
        # 빈 파일에서 시작해도 첫 색은 늘 같다(이름 해시)
        q1, q2 = self.dir / "q1.json", self.dir / "q2.json"
        self.assertEqual(config.assign_colors({"zeta"}, q1), config.assign_colors({"zeta"}, q2))

    def test_colors_through_env_and_state(self):
        p = self.dir / "c.json"
        home = self.dir / "home"
        self.session(home, "b-1", str(home / "work/blog"))
        with mock.patch.dict(os.environ, {"KITTY_OFFICE_CONFIG": str(p)}):
            cols = config.assign_colors({"blog"})
            cfg = config.load()
        self.assertEqual(build_state(home, NOW, cfg)["sessions"][0]["color"], cols["blog"])

    def test_colors_forget_folders_unseen_30_days(self):
        from datetime import date
        p = self.dir / "c.json"
        d0, d20, d40 = date(2026, 10, 1), date(2026, 10, 21), date(2026, 11, 10)
        cols = config.assign_colors({"old", "keep"}, p, today=d0)
        config.assign_colors({"keep"}, p, today=d20)                       # keep 만 20일째에 다시 보였다
        mtime = p.stat().st_mtime_ns
        self.assertEqual(config.assign_colors({"keep"}, p, today=d20), {"old": cols["old"], "keep": cols["keep"]})
        self.assertEqual(p.stat().st_mtime_ns, mtime)                       # 같은 날 같은 폴더 — 바뀐 게 없으면 파일을 안 쓴다
        after = config.assign_colors(set(), p, today=d40)                   # old 는 40일째 안 보임(뺌), keep 은 20일째(남김)
        self.assertEqual(after, {"keep": cols["keep"]})
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(data["colors_seen"], {"keep": "2026-10-21"})
        self.assertEqual(data["colors"], {"keep": cols["keep"]})            # colors 값 모양은 그대로 {폴더: 색}

    def test_cats_go_out_in_state(self):
        home = self.dir / "home"
        self.session(home, "b-1", str(home / "work/blog"))
        self.assertEqual(build_state(home, NOW, {**config.DEFAULT, "cats": {"alice": "ginger"}})["cats"], {"alice": "ginger"})
        self.assertEqual(build_state(home, NOW, config.DEFAULT)["cats"], {})   # 없으면 빈 표 — 화면이 이름 해시로

    def test_weather_goes_out_only_when_set(self):
        home = self.dir / "home"
        self.assertIsNone(build_state(home, NOW, config.DEFAULT)["weather"])  # 기본은 끔 — 화면이 밖에 묻지 않는다
        w = {"lat": 37.5, "lon": 127.0}
        self.assertEqual(build_state(home, NOW, {**config.DEFAULT, "weather": w})["weather"], w)

    def test_old_knock_office_folders_are_reused(self):
        """새 이름(kitty-office) 폴더가 없고 옛 이름(knock-office) 폴더가 있으면 옛 것을 그대로 쓴다. 둘 다 있으면 새 것."""
        home = self.dir / "h"
        new_c, old_c = home / ".config/kitty-office", home / ".config/knock-office"
        new_k, old_k = home / "Library/Caches/kitty-office", home / "Library/Caches/knock-office"
        env = {k: v for k, v in os.environ.items() if k != "KITTY_OFFICE_CONFIG"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(Path, "home", lambda: home):
            home.mkdir()
            self.assertEqual((config.path(), config.cache_dir()), (new_c / "config.json", new_k))   # 둘 다 없음 → 새
            old_c.mkdir(parents=True); old_k.mkdir(parents=True)
            self.assertEqual((config.path(), config.cache_dir()), (old_c / "config.json", old_k))   # 옛 것만 → 옛
            new_c.mkdir(parents=True); new_k.mkdir(parents=True)
            self.assertEqual((config.path(), config.cache_dir()), (new_c / "config.json", new_k))   # 둘 다 → 새
            new_c.rmdir(); new_k.rmdir(); old_c.rmdir(); old_k.rmdir(); new_c.mkdir(parents=True); new_k.mkdir(parents=True)
            self.assertEqual((config.path(), config.cache_dir()), (new_c / "config.json", new_k))   # 새 것만 → 새
            self.assertFalse(old_c.exists())                                                         # 옮기거나 만들지 않는다

    def test_notify_lang_follows_macos_unless_set(self):
        class R:
            def __init__(self, out): self.stdout = out
        def fake(out):
            return lambda *a, **k: R(out)
        def broken(*a, **k):
            raise OSError("no defaults")
        self.assertEqual(config.system_lang(fake('(\n    "ko-KR",\n    "en-US"\n)\n')), "ko")
        self.assertEqual(config.system_lang(fake('(\n    "en-US",\n    "ko-KR"\n)\n')), "en")
        self.assertEqual(config.system_lang(fake("")), "en")
        self.assertEqual(config.system_lang(broken), "en")
        with mock.patch.object(config, "system_lang", lambda: "en"):
            self.assertEqual(config.notify_lang({**config.DEFAULT}), "en")              # 설정 없으면 macOS 언어
            self.assertEqual(config.notify_lang({**config.DEFAULT, "lang": "ko"}), "ko") # 설정이 우선
        self.assertIsNone(config.DEFAULT["lang"])

    def test_colors_wrap_when_palette_runs_out(self):
        p = self.dir / "c.json"
        many = {f"f{i}" for i in range(len(config.PALETTE) + 5)}
        cols = config.assign_colors(many, p)
        self.assertEqual(len(cols), len(many))
        self.assertEqual(len(set(cols.values())), len(config.PALETTE))     # 팔레트를 다 쓰고 나서야 겹친다


class TestLock(unittest.TestCase):
    """token·allowedHosts 가 걸린 서버를 임시 포트로 띄워 요청을 보낸다."""

    def start(self, **cfg):
        srv = server.make_server({**config.DEFAULT, **cfg}, port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.port = srv.server_address[1]
        lk = mock.patch.object(server, "LOCKOUT", server.Lockout())  # 테스트끼리 틀린 횟수가 섞이지 않게
        lk.start()
        self.addCleanup(lk.stop)
        # 진짜 ~/.claude 를 읽지 않게
        for name, val in (("build_state", lambda *a: {"sessions": []}), ("load_sessions", lambda home: []), ("folders", lambda home: set())):
            p = mock.patch.object(server, name, val)
            p.start()
            self.addCleanup(p.stop)

        self.todo_file = Path(tempfile.mkdtemp()) / "todo.json"  # 진짜 설정 폴더의 todo.json 을 읽지도 쓰지도 않게
        p = mock.patch.object(server.todo, "path", lambda: self.todo_file)
        p.start()
        self.addCleanup(p.stop)

    def req(self, method, path, headers=None, body=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": host or f"127.0.0.1:{self.port}", **(headers or {})}
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        out = (r.status, dict(r.getheaders()), r.read())
        c.close()
        return out

    def post(self, cookie=None, origin=None, host=None):
        host = host or f"127.0.0.1:{self.port}"
        h = {"Origin": origin or f"http://{host}", "Content-Type": "application/json"}
        if cookie:
            h["Cookie"] = cookie
        return self.req("POST", "/api/focus", h, json.dumps({"id": "x"}), host=host)[0]

    def test_island_rename_writes_config(self):
        p = Path(tempfile.mkdtemp()) / "c.json"
        with mock.patch.dict(os.environ, {"KITTY_OFFICE_CONFIG": str(p)}):
            self.start()
            h = {"Origin": f"http://127.0.0.1:{self.port}", "Content-Type": "application/json"}
            ok = self.req("POST", "/api/island", h, json.dumps({"folders": ["ai"], "name": "AI 연구"}))[0]
            bad = self.req("POST", "/api/island", h, json.dumps({"folders": [], "name": "x"}))[0]
            evil = self.req("POST", "/api/island", {**h, "Origin": "http://evil.example.com"}, json.dumps({"folders": ["ai"], "name": "x"}))[0]
        self.assertEqual((ok, bad, evil), (200, 400, 403))
        self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["islands"], {"ai": "AI 연구"})

    def test_exit_refuses_busy_session(self):
        """세션 끝내기 — 일하는 중·회의 중이면 서버가 거절하고 터미널을 건드리지 않는다(화면만 믿지 않는다)."""
        self.start()
        h = {"Origin": f"http://127.0.0.1:{self.port}", "Content-Type": "application/json"}
        sess = [{"sessionId": "x", "pid": 4242, "status": "idle", "name": "x", "cwd": "/tmp"}]
        calls = []
        with mock.patch.object(server, "load_sessions", lambda home: sess), \
             mock.patch.object(server.term, "tab_of", lambda pid: ("cmux", "ws", "sf")), \
             mock.patch.object(server.term, "exit_session", lambda tab, pid=None: calls.append((tab, pid))):
            for state, want in (("working", 409), ("meeting", 409), ("reported", 200)):
                with mock.patch.object(server, "build_state", lambda *a, st=state: {"sessions": [{"id": "x", "state": st}]}):
                    self.assertEqual(self.req("POST", "/api/exit", h, json.dumps({"id": "x"}))[0], want, state)
            sess[0]["status"] = "waiting"  # 승인·선택 창이 떠 있으면 /exit 글자가 선택지를 누를 수 있다
            self.assertEqual(self.req("POST", "/api/exit", h, json.dumps({"id": "x"}))[0], 409)
        self.assertEqual(calls, [(("cmux", "ws", "sf"), 4242)])  # 보고 상태 한 번만 실제로 불렸다

    def test_messages_follow_language(self):
        """사람에게 보이는 문구 — X-Lang · ?lang · Accept-Language 순으로 고른다(기본 ko)."""
        self.start(token="s3cret")
        ko = self.req("GET", "/", {"Accept-Language": "ko-KR,ko;q=0.9"})[2].decode()
        en = self.req("GET", "/", {"Accept-Language": "en-US,en;q=0.9"})[2].decode()
        self.assertIn("키를 아는 사람만", ko)
        self.assertIn("Only people with the key", en)
        self.assertNotRegex(en, "[가-힣]")
        self.assertIn("Only people", self.req("GET", "/?lang=en", {"Accept-Language": "ko"})[2].decode())
        h = {"Origin": f"http://127.0.0.1:{self.port}", "Content-Type": "application/json", "Cookie": "knock_token=s3cret"}
        code, _, body = self.req("POST", "/api/nope", {**h, "X-Lang": "en"}, "{}")
        self.assertEqual((code, json.loads(body)["error"]), (404, "Not found"))
        self.assertEqual(json.loads(self.req("POST", "/api/nope", h, "{}")[2])["error"], "없는 주소")

    def test_todo_api(self):
        """할 일 — 추가·체크·고치기·지우기, 잘못된 글은 400, 기존 POST 보호(Origin·Content-Type)를 그대로 탄다, /api/state 에 요약."""
        self.start()
        h = {"Origin": f"http://127.0.0.1:{self.port}", "Content-Type": "application/json"}
        post = lambda b, hh=h: self.req("POST", "/api/todo", hh, json.dumps(b))
        code, _, body = post({"op": "add", "text": "  우유 사기  "})
        items = json.loads(body)["items"]
        self.assertEqual((code, items[0]["text"], items[0]["done"]), (200, "우유 사기", False))
        tid = items[0]["id"]
        self.assertTrue(json.loads(post({"op": "toggle", "id": tid})[2])["items"][0]["done"])
        self.assertEqual(json.loads(post({"op": "edit", "id": tid, "text": "두유 사기"})[2])["items"][0]["text"], "두유 사기")
        self.assertEqual(json.loads(self.req("GET", "/api/todo")[2])["items"][0]["text"], "두유 사기")
        self.assertEqual(json.loads(self.req("GET", "/api/state")[2])["todo"], {"done": 1, "total": 1, "late": 0, "past": 0})
        self.assertEqual(post({"op": "add", "text": ""})[0], 400)
        self.assertEqual(post({"op": "toggle", "id": "nope"})[0], 400)
        self.assertEqual(post({"op": "add", "text": "x"}, {**h, "Origin": "http://evil.example.com"})[0], 403)
        self.assertEqual(post({"op": "add", "text": "x"}, {"Origin": h["Origin"], "Content-Type": "text/plain"})[0], 403)
        self.assertEqual(json.loads(post({"op": "delete", "id": tid})[2])["items"], [])
        self.assertEqual(json.loads(self.todo_file.read_text(encoding="utf-8")), {"items": []})

    def test_missing_sound_is_empty_not_404(self):
        self.start()
        self.assertEqual(self.req("GET", "/sounds/none-such.mp3")[0], 204)
        self.assertEqual(self.req("GET", "/sounds/fire.mp3")[0], 200)
        self.assertEqual(self.req("GET", "/none-such.txt")[0], 404)  # 소리 밖은 그대로

    def test_no_token_is_open(self):
        self.start()
        self.assertEqual(self.req("GET", "/")[0], 200)
        self.assertEqual(self.req("GET", "/api/state")[0], 200)

    R = "myhost.example.com:8444"  # 원격으로 연 이름

    def test_token_locks_local_too(self):
        """token 이 있으면 127.0.0.1 도 키를 묻는다 — Host 를 127.0.0.1 로 바꾸는 프록시 뒤에서도 잠기게."""
        self.start(token="s3cret")
        self.assertEqual(self.req("GET", "/api/state")[0], 401)
        self.assertEqual(self.req("GET", "/api/state", host=f"localhost:{self.port}")[0], 401)
        self.assertEqual(self.post(), 401)
        h = {"Origin": f"http://127.0.0.1:{self.port}", "Content-Type": "application/json"}
        code, headers, _ = self.req("POST", "/api/login", h, json.dumps({"token": "s3cret"}))
        self.assertEqual(code, 200)
        self.assertNotIn("Secure", headers["Set-Cookie"])  # http 로 연 로컬에 Secure 쿠키는 안 실린다
        self.assertEqual(self.post("knock_token=s3cret"), 409)  # 잠금 통과 — 세션이 없어 409

    def test_token_link_and_header_are_rate_limited(self):
        """?token= 과 X-Token 도 로그인처럼 틀린 횟수를 센다 — 다섯 번 틀리면 맞는 키도 막힌다."""
        self.start(token="s3cret", allowedHosts=[self.R])
        with mock.patch.object(server.time, "sleep", lambda s: None):
            for _ in range(5):
                self.assertEqual(self.req("GET", "/?token=wrong", host=self.R)[0], 200)  # 키 입력 화면
            self.assertEqual(self.req("GET", "/?token=s3cret", host=self.R)[0], 429)
        self.start(token="s3cret", allowedHosts=[self.R])
        with mock.patch.object(server.time, "sleep", lambda s: None):
            for _ in range(5):
                self.assertEqual(self.req("GET", "/api/state", {"X-Token": "wrong"}, host=self.R)[0], 401)
            self.assertEqual(self.req("GET", "/api/state", {"X-Token": "s3cret"}, host=self.R)[0], 429)

    def test_remote_is_locked(self):
        self.start(token="s3cret", allowedHosts=[self.R])
        code, _, body = self.req("GET", "/", host=self.R)
        self.assertEqual(code, 200)
        self.assertIn(b"api/login", body)  # 키 입력 화면
        for path in ("/api/state", "/lines.ko.json", "/?token=wrong"):
            self.assertIn(self.req("GET", path, host=self.R)[0], (401, 200) if path.startswith("/?") else (401,), path)
        self.assertEqual(self.post(host=self.R, origin=f"https://{self.R}"), 401)

    def test_login_form_sets_cookie(self):
        self.start(token="s3cret", allowedHosts=[self.R])
        h = {"Origin": f"https://{self.R}", "Content-Type": "application/json"}
        self.assertEqual(self.req("POST", "/api/login", h, json.dumps({"token": "nope"}), host=self.R)[0], 401)
        code, headers, _ = self.req("POST", "/api/login", h, json.dumps({"token": "s3cret"}), host=self.R)
        self.assertEqual(code, 200)
        for part in ("knock_token=s3cret", "HttpOnly", "SameSite=Strict", "Secure"):
            self.assertIn(part, headers["Set-Cookie"])
        self.assertEqual(self.req("GET", "/api/state", {"Cookie": "knock_token=s3cret"}, host=self.R)[0], 200)
        evil = {**h, "Origin": "https://evil.example.com"}
        self.assertEqual(self.req("POST", "/api/login", evil, json.dumps({"token": "s3cret"}), host=self.R)[0], 401)

    def test_token_link_sets_cookie(self):
        self.start(token="s3cret", allowedHosts=[self.R])
        code, headers, body = self.req("GET", "/?token=s3cret", host=self.R)
        self.assertEqual(code, 200)
        self.assertIn("knock_token=s3cret", headers["Set-Cookie"])
        self.assertIn(b"location.replace", body)
        self.assertEqual(self.req("GET", "/", {"X-Token": "s3cret"}, host=self.R)[0], 200)
        self.assertEqual(self.req("GET", "/api/state", {"Cookie": "knock_token=nope"}, host=self.R)[0], 401)

    def test_post_origin_and_host(self):
        self.start(token="s3cret", allowedHosts=["myhost.example.com:8444"])
        ck = "knock_token=s3cret"
        self.assertEqual(self.post(ck), 409)  # 잠금 통과 — 세션이 없어 409
        self.assertEqual(self.post(ck, origin="http://evil.example.com"), 403)
        remote = "myhost.example.com:8444"
        self.assertEqual(self.post(ck, origin=f"https://{remote}", host=remote), 409)
        self.assertEqual(self.post(ck, origin="https://evil.example.com", host="evil.example.com"), 403)
        self.assertEqual(self.req("GET", "/", {"Cookie": ck}, host="evil.example.com")[0], 403)


if __name__ == "__main__":
    unittest.main()


class TestLockout(unittest.TestCase):
    def test_five_wrong_blocks_that_ip_only(self):
        lk = server.Lockout()
        for i in range(4):
            lk.fail("1.1.1.1", 100 + i)
        self.assertEqual(lk.blocked("1.1.1.1", 105), 0)
        lk.fail("1.1.1.1", 105)
        self.assertGreater(lk.blocked("1.1.1.1", 106), 0)
        self.assertEqual(lk.blocked("2.2.2.2", 106), 0)
        self.assertEqual(lk.blocked("1.1.1.1", 106 + lk.IP_BLOCK), 0)

    def test_many_ips_block_everyone(self):
        lk = server.Lockout()
        for i in range(lk.TOTAL + 1):
            lk.fail(f"9.9.9.{i}", 200 + i)
        self.assertGreater(lk.blocked("3.3.3.3", 300), 0)
