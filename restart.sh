#!/usr/bin/env bash
# 로컬 개발용 — Kitty Office 서버를 포그라운드로 띄운다.
# 1) 5430 을 듣고 있는 기존 Kitty Office 를 끈다(남의 프로세스면 멈춘다)  2) 서버를 띄운다
# 3) office/·web/·설정 파일($KITTY_OFFICE_CONFIG, 없으면 ~/.config/kitty-office/config.json — 그 폴더가 없고 옛 ~/.config/knock-office 가 있으면 옛 것)이 바뀌면 2초 안에 다시 띄운다  4) Ctrl+C 로 같이 끝난다
# 표준 라이브러리만 쓰므로 감시도 파일 수정 시각을 2초마다 비교하는 루프로 한다.
set -u
cd "$(dirname "$0")"
PORT="${OFFICE_PORT:-5430}"
CONFIG_DIR="$HOME/.config/kitty-office"; [ ! -e "$CONFIG_DIR" ] && [ -e "$HOME/.config/knock-office" ] && CONFIG_DIR="$HOME/.config/knock-office" # 옛 이름(Knock Office) 폴더를 이어 쓴다
CONFIG="${KITTY_OFFICE_CONFIG:-$CONFIG_DIR/config.json}"

pids=$(lsof -ti tcp:"$PORT" -sTCP:LISTEN || true)
if [ -n "$pids" ]; then
  # 남의 프로그램이 그 포트를 쓰고 있으면 죽이지 않고 멈춘다
  for pid in $pids; do
    case "$(ps -p "$pid" -o command= 2>/dev/null)" in
      *office.server*) ;;
      *) echo "포트 $PORT 를 Kitty Office 가 아닌 프로세스(pid $pid)가 쓰고 있다 — 그 프로그램을 끄거나 OFFICE_PORT 로 다른 포트를 고르세요"; exit 1 ;;
    esac
  done
  echo "포트 $PORT 쓰는 Kitty Office 종료: $pids"
  kill $pids 2>/dev/null; sleep 1
  pids=$(lsof -ti tcp:"$PORT" -sTCP:LISTEN || true)
  [ -n "$pids" ] && kill -9 $pids 2>/dev/null
fi

stamp() { find office web "$CONFIG" -type f ! -path '*/__pycache__/*' -exec stat -f '%m %N' {} + 2>/dev/null | md5; }

child=""
stop() { [ -n "$child" ] && kill "$child" 2>/dev/null; wait "$child" 2>/dev/null; exit 0; }
trap stop INT TERM

while true; do
  OFFICE_PORT="$PORT" python3 -m office.server &
  child=$!
  last=$(stamp)
  while kill -0 "$child" 2>/dev/null; do
    sleep 2
    [ "$(stamp)" != "$last" ] && { echo "소스가 바뀌어 다시 띄운다"; break; }
  done
  if kill -0 "$child" 2>/dev/null; then
    kill "$child" 2>/dev/null; wait "$child" 2>/dev/null
  else
    # 서버가 스스로 죽었다(코드 오류 등) — 고칠 때까지 기다렸다가 다시 띄운다
    echo "서버가 멈췄다 — 소스가 바뀌면 다시 띄운다"
    while [ "$(stamp)" = "$last" ]; do sleep 2; done
  fi
done
