#!/usr/bin/env bash
# Nightly backup of Qwen Desktop: a consistent copy of the database (SQLite online backup, safe while the
# servers run), uploaded files, workspaces, connected repos, chat scratch folders, TLS certs, config and a
# git bundle of the code. One .tgz per run in $BACKUP_DIR; the newest $KEEP are kept.
#
#   scripts/backup.sh            run now
#   KEEP=30 scripts/backup.sh    keep more
# Restore: see "Backups" in README.md.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-/home/tmc/qwen-desktop-backups/nightly}"
KEEP="${KEEP:-14}"
DATA="$ROOT/data"
DB="$DATA/qwen-desktop.db"
PY="$ROOT/backend/.venv/bin/python"

log() { echo "$(date '+%F %T') backup: $*"; }

mkdir -p "$BACKUP_DIR"
exec 9>"$BACKUP_DIR/.lock"
flock -n 9 || { log "another backup is running, skipping"; exit 0; }

stamp="$(date +%Y%m%d-%H%M%S)"
stage="$(mktemp -d "$BACKUP_DIR/.stage-$stamp-XXXX")"
trap 'rm -rf "$stage"' EXIT
mkdir -p "$stage/data"

# 1. Database: online backup API (consistent snapshot, includes the WAL), then verify it.
"$PY" - "$DB" "$stage/data/qwen-desktop.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=30)
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
src.close()
ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
dst.close()
if ok != "ok":
    sys.exit(f"integrity check failed: {ok}")
PY

# 2. Files (logs are left out: they rotate on their own and can be large).
for d in uploads workspaces repos sandbox certs; do
  [ -d "$DATA/$d" ] && cp -a "$DATA/$d" "$stage/data/"
done
cp -a "$ROOT/config.yaml" "$stage/"

# 3. Code: full git history plus any uncommitted changes as a patch.
if git -C "$ROOT" rev-parse --verify -q HEAD >/dev/null; then
  git -C "$ROOT" bundle create -q "$stage/code.bundle" --all
  git -C "$ROOT" diff HEAD --binary > "$stage/uncommitted.patch"
  [ -s "$stage/uncommitted.patch" ] || rm "$stage/uncommitted.patch"
fi

out="$BACKUP_DIR/qwen-desktop-$stamp.tgz"
tar -C "$stage" -czf "$out.part" .
mv "$out.part" "$out"
chmod 600 "$out"     # holds the TLS key, password hashes and API key hashes
log "wrote $out ($(du -h "$out" | cut -f1))"

# 4. Retention: keep the newest $KEEP archives.
ls -1t "$BACKUP_DIR"/qwen-desktop-*.tgz 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do
  rm -f -- "$old" && log "pruned $(basename "$old")"
done
