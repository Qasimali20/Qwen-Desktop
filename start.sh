#!/usr/bin/env bash
# Qwen Desktop launcher.
#   ./start.sh            build frontend if needed, serve http :8300 and https :8343
#   ./start.sh dev        backend with --reload + Vite dev server (:5173)
#   ./start.sh stop       stop running servers
set -euo pipefail
cd "$(dirname "$0")"
ROOT=$PWD
HTTP_PORT=${HTTP_PORT:-8300}
HTTPS_PORT=${HTTPS_PORT:-8343}
LOG_DIR=$ROOT/data/logs
mkdir -p "$LOG_DIR"
UVICORN="$ROOT/backend/.venv/bin/uvicorn app.main:app --app-dir $ROOT/backend --host 0.0.0.0 --log-config $ROOT/backend/logging.yaml"
LOG_MAX_BYTES=$((20 * 1024 * 1024))
LOG_KEEP=5

# Logs are appended across restarts (timestamped by backend/logging.yaml). A log over LOG_MAX_BYTES is
# rotated at start-up: http.log -> http.log.1 -> … -> http.log.$LOG_KEEP (the oldest is dropped).
rotate() {
  local f=$1
  [[ -f $f && $(stat -c %s "$f") -gt $LOG_MAX_BYTES ]] || return 0
  rm -f "$f.$LOG_KEEP"
  for ((i = LOG_KEEP - 1; i >= 1; i--)); do [[ -f $f.$i ]] && mv "$f.$i" "$f.$((i + 1))"; done
  mv "$f" "$f.1"
}

stop() {
  for p in "$HTTP_PORT" "$HTTPS_PORT" 5173; do fuser -k "$p/tcp" >/dev/null 2>&1 || true; done
}

case "${1:-prod}" in
  stop) stop; echo "stopped"; exit 0 ;;
  dev)
    stop
    $UVICORN --port "$HTTP_PORT" --reload --reload-dir "$ROOT/backend/app" &
    (cd frontend && exec npm run dev) &
    trap 'stop' INT TERM
    wait ;;
  prod)
    stop
    if ! docker image inspect qwen-desktop-sandbox:3 >/dev/null 2>&1; then
      echo "note: analysis sandbox image missing — build it with ./sandbox/build.sh (data analysis disabled until then)"
    fi
    if [[ ! -f frontend/dist/index.html || -n "$(find frontend/src frontend/index.html -newer frontend/dist/index.html -print -quit)" ]]; then
      (cd frontend && npm run build >/dev/null) && echo "frontend built"
    fi
    rotate "$LOG_DIR/http.log"; rotate "$LOG_DIR/https.log"
    echo "$(date '+%F %T') ----- start ($(whoami))" | tee -a "$LOG_DIR/http.log" >>"$LOG_DIR/https.log"
    nohup $UVICORN --port "$HTTP_PORT" --workers 1 --timeout-keep-alive 75 >>"$LOG_DIR/http.log" 2>&1 &
    if [[ -f data/certs/cert.pem ]]; then
      nohup $UVICORN --port "$HTTPS_PORT" --ssl-keyfile data/certs/key.pem --ssl-certfile data/certs/cert.pem \
        >>"$LOG_DIR/https.log" 2>&1 &
    fi
    sleep 2
    echo "Qwen Desktop:"
    echo "  http://localhost:$HTTP_PORT"
    echo "  https://$(ip route get 1.1.1.1 2>/dev/null | awk "{for(i=1;i<=NF;i++) if(\$i==\"src\") print \$(i+1)}"):$HTTPS_PORT   (use this from other machines; needed for the mic)"
    echo "  logs: $LOG_DIR" ;;
esac
