"""설정 파일 — $KITTY_OFFICE_CONFIG 가 있으면 그것, 없으면 ~/.config/kitty-office/config.json(그 폴더가 없고 옛 ~/.config/knock-office 가 있으면 옛 것).
파일이 없으면 기본값(결재실 + 로비, 모든 세션이 로비, 캐릭터는 g1~g4)으로 돈다.
모양은 저장소의 config.example.json 을 본다."""
import json
import math
import os
import subprocess
import zlib
from datetime import date
from pathlib import Path

LOBBY = {"id": "lobby", "name": "로비", "order": -1, "prefixes": []}
DEFAULT = {
    "title": "Kitty Office",
    "floors": [LOBBY],   # 결재실(boss)은 코드에 고정 — 여기 적지 않는다
    "seats": {},         # 폴더 → 캐릭터
    "names": {},         # 세션 이름 앞부분 → 캐릭터. 거꾸로 캐릭터 → 표시 이름으로도 쓴다
    "notify": True,      # 새 기다림이 생기면 맥 알림
    "allowedHosts": [],  # 원격으로 열 때 받을 Host(예: "myhost.example.com:8444"). 127.0.0.1·localhost 는 늘 허용. 적으면 token 필수
    "token": None,       # 있으면 로컬까지 쿠키·X-Token 없는 요청을 막는다. 16자 이상
    "lang": None,        # 맥 알림 문구 언어(ko·en). 없으면 macOS 언어(system_lang)
    "weather": None,     # {"lat": .., "lon": ..} 이면 화면이 그 좌표 날씨를 open-meteo 에 묻는다. 없으면 외부 요청 없음
    "islands": {},       # 폴더 이름 → 화면 섬 이름. 같은 이름을 준 폴더끼리 한 섬이 된다(화면 ✎ 로 고친다)
    "colors": {},        # 폴더 이름 → 목록 색("#rrggbb"). 서버가 처음 본 폴더에 정해 적는다(assign_colors)
    "cats": {},          # 캐릭터 → 고양이 털 종류(mackerel·tuxedo·ginger·siamese·calico·patch·gray·black·brown·cream). 없으면 이름 해시
}
TOKEN_MIN = 16


def home_dir(new, old):
    """이름을 Kitty Office 로 바꾸기 전(Knock Office)의 폴더를 이어 쓴다 — 새 폴더가 없고 옛 폴더가 있으면 옛 것(옮기지 않는다)."""
    n, o = Path.home() / new, Path.home() / old
    return o if not n.exists() and o.exists() else n


def path():
    return Path(os.environ.get("KITTY_OFFICE_CONFIG") or home_dir(".config/kitty-office", ".config/knock-office") / "config.json")


def system_lang(run=subprocess.run):
    """macOS 언어 설정 첫 값이 ko 로 시작하면 ko, 아니면 en. 읽지 못하면 en."""
    try:
        out = run(["defaults", "read", "-g", "AppleLanguages"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return "en"
    first = next((x.strip().strip('",') for x in out.splitlines() if x.strip().strip('",()')), "")
    return "ko" if first.lower().startswith("ko") else "en"


def notify_lang(cfg):
    """알림 언어 — 설정 lang 이 ko·en 이면 그것, 아니면 macOS 언어."""
    return cfg.get("lang") if cfg.get("lang") in ("ko", "en") else system_lang()


def cache_dir():
    """요약 파일(wait.json)·첨부를 두는 곳 — 설정 폴더와 같은 규칙."""
    return home_dir("Library/Caches/kitty-office", "Library/Caches/knock-office")


def load(p=None):
    """없으면 기본값. 있는데 깨졌으면 ValueError — 조용히 기본값으로 돌면 층이 왜 사라졌는지 모른다."""
    p = Path(p) if p else path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"{p}: 최상위는 JSON 객체여야 한다")
    cfg = {**DEFAULT, **data}
    if cfg["allowedHosts"] and not cfg["token"]:
        raise ValueError(f"{p}: allowedHosts 를 적었으면 token 도 있어야 한다 — 키 없이 원격에 열면 남이 터미널에 글을 넣는다")
    if cfg["token"] and len(str(cfg["token"])) < TOKEN_MIN:
        raise ValueError(f"{p}: token 은 {TOKEN_MIN}자 이상이어야 한다 — python3 -c \"import secrets; print(secrets.token_urlsafe(24))\"")
    if not any(f.get("id") == "lobby" for f in cfg["floors"]):
        cfg["floors"] = [*cfg["floors"], LOBBY]
    return cfg


def save_islands(names, p=None):
    """화면에서 고친 섬 이름을 설정 파일에 쓴다. {폴더: 이름}, 이름이 비면 지운다. 다른 칸은 손대지 않는다."""
    p = Path(p) if p else path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    isl = {**data.get("islands", {})}
    for folder, name in names.items():
        if name:
            isl[folder] = name
        else:
            isl.pop(folder, None)
    data["islands"] = isl
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return isl


# 밝은 배경에서 서로 갈리는 40색 — CIELAB 에서 먼 것부터 고른 순서. 결재 빨강(#d9472b) 근처(ΔE 35 안)와 아주 옅은 색은 뺐다
PALETTE = ["#aa3c56", "#31ba94", "#1a8bdd", "#9e8429", "#d48dd4", "#186d78", "#6a56a5", "#197235", "#8fb15e", "#e39362",
           "#27b3de", "#f58498", "#1b6996", "#187058", "#4a6d1a", "#a33e74", "#8e4990", "#28b8b2", "#b099eb", "#68b776",
           "#eb85b7", "#696606", "#835c0b", "#2963b1", "#35aff7", "#b87834", "#757fd7", "#169963", "#d45f88", "#7ea5f7",
           "#b0a852", "#589546", "#7e8e30", "#cd9e53", "#c465a8", "#a572c4", "#4db4c3", "#2c8fbc", "#299685", "#3d9297"]


def _lab(h):
    rgb = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = [u / 12.92 if u <= 0.04045 else ((u + 0.055) / 1.055) ** 2.4 for u in rgb]
    x, y, z = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047, 0.2126 * r + 0.7152 * g + 0.0722 * b, (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883
    f = lambda t: t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
    return 116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))


def _pick(folder, visible, used):
    """지금 보이는 폴더 색은 피하고, 이미 쓰인 색들과 가장 먼 팔레트 색. 동점이면 폴더 이름 해시(파일이 비어도 늘 같은 첫 색)."""
    h = zlib.crc32(folder.encode("utf-8"))
    cands = [c for c in PALETTE if c not in visible]
    if not cands:  # 팔레트를 다 썼다 — 겹침 허용
        return PALETTE[h % len(PALETTE)]
    cands = [c for c in cands if c not in used] or cands
    ref = [_lab(c) for c in used | visible]
    score = {c: round(min((math.dist(_lab(c), r) for r in ref), default=0), 3) for c in cands}
    top = max(score.values())
    ties = [c for c in cands if score[c] == top]
    return ties[h % len(ties)]


COLOR_KEEP_DAYS = 30  # 이만큼 안 보인 폴더는 colors 에서 뺀다(그 색을 새 폴더가 다시 쓸 수 있게)


def assign_colors(folders, p=None, today=None):
    """폴더마다 목록 색. 이미 있는 폴더는 그대로, 처음 본 폴더만 정한다. 보인 날은 colors_seen 에 날짜로 적고,
    30일 넘게 안 보인 폴더는 colors·colors_seen 에서 뺀다. 바뀐 게 있을 때만 설정 파일을 쓴다. {폴더: 색} 전체를 돌려준다."""
    p = Path(p) if p else path()
    today = today or date.today()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    colors, seen = {**data.get("colors", {})}, {**data.get("colors_seen", {})}
    day = today.isoformat()
    for f in colors:
        seen.setdefault(f, day)  # 날짜 칸이 생기기 전부터 있던 폴더는 오늘부터 센다
    for f in folders:
        if f and f in colors:
            seen[f] = day
    old = {f for f, d in seen.items() if f not in folders and (today - date.fromisoformat(d)).days > COLOR_KEEP_DAYS}
    for f in old:
        colors.pop(f, None)
        seen.pop(f, None)
    visible = {colors[f] for f in folders if f in colors}
    for f in sorted({f for f in folders if f and f not in colors}):
        colors[f] = _pick(f, visible, set(colors.values()))
        visible.add(colors[f])
        seen[f] = day
    seen = {f: d for f, d in seen.items() if f in colors}
    if colors == data.get("colors", {}) and seen == data.get("colors_seen", {}):
        return colors
    data["colors"], data["colors_seen"] = colors, seen
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return colors
