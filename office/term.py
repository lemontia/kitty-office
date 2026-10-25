"""터미널 조종 — 세션이 떠 있는 터미널 창을 찾아 그 창으로 가거나 입력창에 답을 넣는다.
claude 프로세스 환경 변수로 어느 터미널의 어느 창인지 안다(2026-10-21 실측):
  cmux  CMUX_WORKSPACE_ID·CMUX_SURFACE_ID     — 가기·답하기
  Orca  ORCA_TERMINAL_HANDLE                  — 가기·답하기
  herdr HERDR_PANE_ID                         — 답하기만(창을 앞으로 띄우는 명령이 탭 안에서만 된다)
iTerm 등 나머지, 또는 명령을 못 찾은 터미널은 None."""
import functools
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path


def _find(name, *fallbacks):
    """PATH 에서 찾고, 없으면 흔한 설치 위치를 본다. 끝내 없으면 None — 그 터미널은 지원 안 함."""
    return shutil.which(name) or next((f for f in map(os.path.expanduser, fallbacks) if os.access(f, os.X_OK)), None)


CMUX = _find("cmux", "/Applications/cmux.app/Contents/Resources/bin/cmux")
ORCA = _find("orca", "/usr/local/bin/orca", "/opt/homebrew/bin/orca")
HERDR = _find("herdr", "~/.local/bin/herdr", "/opt/homebrew/bin/herdr", "/usr/local/bin/herdr")


_herdr_cache = [0.0, {}]


def _herdr_panes():
    """윈도우용 — herdr pane list 의 agent_session(=Claude Code sessionId) → pane_id. 3초 동안 재사용."""
    if time.monotonic() - _herdr_cache[0] < 3:
        return _herdr_cache[1]
    out = {}
    try:
        r = subprocess.run([HERDR, "pane", "list"], capture_output=True, encoding="utf-8", errors="replace", timeout=5)
        for p in json.loads(r.stdout)["result"]["panes"]:
            sid = (p.get("agent_session") or {}).get("value")
            if sid:
                out[sid] = p["pane_id"]
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        pass
    _herdr_cache[:] = [time.monotonic(), out]
    return out


def _tab_of_nt(pid):
    """윈도우엔 ps eww 가 없다 — 세션 파일에서 sessionId 를 읽고 herdr 창 목록과 맞춘다."""
    if not HERDR:
        return None
    try:
        sid = json.loads((Path.home() / ".claude/sessions" / f"{int(pid)}.json").read_text(encoding="utf-8")).get("sessionId")
    except (OSError, ValueError, TypeError):
        return None
    pane = _herdr_panes().get(sid)
    return ("herdr", pane) if pane else None


_procs_cache = [0.0, {}]


def _procs():
    """모든 프로세스의 {pid: (부모 pid, 명령줄)}. 5초 동안 재사용 — 윈도우는 한 번 묻는 데 1초쯤 걸린다."""
    if time.monotonic() - _procs_cache[0] < 5:
        return _procs_cache[1]
    out = {}
    try:
        if os.name == "nt":
            ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-CimInstance Win32_Process | "
                  "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress")
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, encoding="utf-8", errors="replace", timeout=10)
            for p in json.loads(r.stdout):
                out[p["ProcessId"]] = (p["ParentProcessId"], p.get("CommandLine") or "")
        else:
            r = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, encoding="utf-8", errors="replace", timeout=10)
            for line in r.stdout.splitlines():
                pid, ppid, cmd = (line.split(None, 2) + [""])[:3]
                out[int(pid)] = (int(ppid), cmd)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        pass
    _procs_cache[:] = [time.monotonic(), out]
    return out


def runs_any(pid, words):
    """pid 아래(자식·손자…)에서 도는 명령줄에 words 중 하나가 들어 있나 — 「뒤에서 셸만 도는」 세션이 실제로 일하는지 가른다."""
    if not words or not pid:
        return False
    procs, todo, seen = _procs(), [int(pid)], set()
    while todo:
        cur = todo.pop()
        for p, (ppid, cmd) in procs.items():
            if ppid == cur and p not in seen:
                seen.add(p)
                if any(w in cmd for w in words):
                    return True
                todo.append(p)
    return False


def tab_of(pid):
    if os.name == "nt":
        return _tab_of_nt(pid)
    return _tab_of_ps(pid)


@functools.lru_cache(maxsize=256)  # ponytail: pid 가 재사용되면 옛 창을 가리킬 수 있다 — 서버 재시작으로 비워진다
def _tab_of_ps(pid):
    try:
        env = subprocess.run(["ps", "eww", "-o", "command=", "-p", str(int(pid))],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    get = lambda k: (m.group(1) if (m := re.search(rf"\b{k}=(\S+)", env)) else None)
    if CMUX and get("CMUX_SURFACE_ID") and get("CMUX_WORKSPACE_ID"):
        return ("cmux", get("CMUX_WORKSPACE_ID"), get("CMUX_SURFACE_ID"))
    if ORCA and get("ORCA_TERMINAL_HANDLE"):
        return ("orca", get("ORCA_TERMINAL_HANDLE"))
    if HERDR and get("HERDR_PANE_ID"):
        return ("herdr", get("HERDR_PANE_ID"))
    return None


def can_focus(tab):
    return bool(tab) and tab[0] in ("cmux", "orca")


def _run(*args):
    r = subprocess.run(list(args), capture_output=True, encoding="utf-8", errors="replace", timeout=15)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"{args[0]} 실패({r.returncode})")
    return r.stdout + r.stderr


def focus(tab):
    if tab[0] == "cmux":
        _, ws, sf = tab
        _run(CMUX, "select-workspace", "--workspace", ws)
        _run(CMUX, "focus-panel", "--panel", sf, "--workspace", ws)
        subprocess.run(["open", "-a", "cmux"], timeout=10)
    elif tab[0] == "orca":
        _run(ORCA, "terminal", "switch", "--terminal", tab[1])
        subprocess.run(["open", "-a", "Orca"], timeout=10)
    else:
        raise RuntimeError("herdr 창은 여기서 앞으로 띄울 수 없다")


def answer(tab, text):
    if tab[0] == "cmux":
        _, ws, sf = tab
        try:
            out = _run(CMUX, "paste", "--workspace", ws, "--surface", sf, "--submit", "--", text)
        except RuntimeError as e:
            if "someone is typing" not in str(e):  # 질문·권한 창이 열린 경우는 그대로 실패
                raise
            # 입력창에 쓰다 만 글이 있다 — 지우고 채팅 글로 바꾼다(되살리기는 그 창에서 Ctrl+Y).
            # Ctrl+C·Esc 는 일하는 중이면 하던 일을 멈춘다 — Ctrl+U(줄 지우기)+Backspace(윗줄로) 만 쓴다(2026-10-23 실측)
            for _ in range(40):
                _run(CMUX, "send-key", "--workspace", ws, "--surface", sf, "ctrl+u")
                _run(CMUX, "send-key", "--workspace", ws, "--surface", sf, "backspace")
            out = _run(CMUX, "paste", "--workspace", ws, "--surface", sf, "--submit", "--", text)
        # 글은 들어갔는데 Enter 가 안 눌렸다는 경고가 올 때가 있다(2026-10-21 실측) — Enter 만 다시 보낸다
        if "submit key was not sent" in out:
            time.sleep(0.5)
            _run(CMUX, "send-key", "--workspace", ws, "--surface", sf, "Enter")
    elif tab[0] == "orca":
        _run(ORCA, "terminal", "send", "--terminal", tab[1], "--text", text, "--enter")
    else:
        _run(HERDR, "pane", "run", tab[1], text)


def stop(tab):
    """하던 일 멈추기 — Claude Code 는 Esc 로 지금 차례를 멈춘다. cmux 는 2026-10-22 cat -v 창에 ^[ 로 들어가는 것 확인.
    Orca 는 Esc 글자를 그대로 보낸다(실측 안 함), herdr 는 send-keys esc(도움말 기준)."""
    if tab[0] == "cmux":
        _, ws, sf = tab
        _run(CMUX, "send-key", "--workspace", ws, "--surface", sf, "escape")
    elif tab[0] == "orca":
        _run(ORCA, "terminal", "send", "--terminal", tab[1], "--text", "\x1b")
    else:
        _run(HERDR, "pane", "send-keys", tab[1], "esc")


def alive(pid):
    """그 pid 의 프로세스가 살아 있나. 윈도우에선 os.kill(pid, 0) 이 확인이 아니라 그 프로세스를 끈다 — OpenProcess 로 본다."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return k.GetLastError() == 5  # 접근 거부 = 남의 프로세스지만 살아 있다
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True  # 남의 프로세스지만 살아 있다
    except OSError:
        return False
    return True


def exit_session(tab, pid=None, wait=8.0):
    """세션 끝내기 — 입력창에 /exit 를 넣어 Claude Code 를 끝내고(쓰다 만 글 지우기는 answer 가 한다),
    그 claude 프로세스(pid)가 사라진 걸 본 뒤에만 그 탭(창 하나)을 닫는다. 워크스페이스·다른 탭은 건드리지 않는다.
    /exit 가 안 들어가면 예외. 탭까지 닫았으면 None, 세션이 안 끝났으면 "still_running", 탭을 못 닫았으면 ("close_failed", 오류) — 문구는 서버가 언어에 맞춰 붙인다."""
    answer(tab, "/exit")
    end = time.monotonic() + wait
    while pid and alive(pid) and time.monotonic() < end:
        time.sleep(0.3)
    if not pid or alive(pid):
        return "still_running"
    try:
        if tab[0] == "cmux":
            _, ws, sf = tab
            try:
                _run(CMUX, "close-surface", "--workspace", ws, "--surface", sf)
            except RuntimeError:  # 셸이 살아 있어 묻는 경우 — claude 는 끝난 걸 확인했으니 그 탭만 강제로
                _run(CMUX, "close-surface", "--workspace", ws, "--surface", sf, "--force")
        elif tab[0] == "orca":
            _run(ORCA, "terminal", "close", "--terminal", tab[1])  # --tab 없이 — 그 창 하나만(나눈 창은 남는다)
        else:
            _run(HERDR, "pane", "close", tab[1])
    except RuntimeError as e:
        return ("close_failed", str(e))
    return None
