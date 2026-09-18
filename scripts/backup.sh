#!/bin/bash
# Automatic snapshot of archTrip's data into data/backups/, only when something changed.
#   - archtrip database (consistent copy via SQLite's backup API), content-hashed so a run
#     without changes stores nothing;
#   - uploaded photos (data/uploads) and a standalone HTML copy of every trip;
#   - keeps the newest KEEP distinct snapshots (default 7); files are prefixed "auto-",
#     manual backups in the same folder are never touched.
# Run it from cron twice a day, e.g.:  0 3,15 * * * /srv/apps/archtrip/scripts/backup.sh >> /srv/apps/archtrip/data/backups/backup.log 2>&1
set -euo pipefail
APP="$(cd "$(dirname "$0")/.." && pwd)"
DATA="$APP/data"; OUT="$DATA/backups"; KEEP="${KEEP:-7}"; PORT="${ARCHTRIP_PORT:-8000}"
mkdir -p "$OUT"
[ -f "$DATA/archtrip.db" ] || { echo "$(date '+%F %R') no hay base de datos en $DATA"; exit 0; }
stamp=$(date +%Y%m%d-%H%M)
tmp="$OUT/.auto-$stamp.db"

python3 - "$DATA/archtrip.db" "$tmp" <<'PY'
import hashlib, sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(src); d = sqlite3.connect(dst); s.backup(d); d.close(); s.close()
h = hashlib.sha256()
for line in sqlite3.connect(dst).iterdump():      # logical content: stable across WAL checkpoints
    h.update(line.encode("utf-8"))
open(dst + ".sha256", "w").write(h.hexdigest())
PY

new=$(cat "$tmp.sha256")
last=$( (ls -1 "$OUT"/auto-*.db.sha256 2>/dev/null || true) | sort | tail -1)
if [ -n "$last" ] && [ "$(cat "$last")" = "$new" ]; then
  rm -f "$tmp" "$tmp.sha256"
  echo "$(date '+%F %R') sin cambios desde $(basename "${last%.db.sha256}")"
  exit 0
fi

mv "$tmp" "$OUT/auto-$stamp.db"; mv "$tmp.sha256" "$OUT/auto-$stamp.db.sha256"
[ -d "$DATA/uploads" ] && tar czf "$OUT/auto-$stamp-uploads.tgz" -C "$DATA" uploads
if curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
  for id in $(curl -s "http://127.0.0.1:$PORT/api/trips" | python3 -c 'import json,sys; print(" ".join(str(t["id"]) for t in json.load(sys.stdin)))'); do
    curl -sf -o "$OUT/auto-$stamp-viaje$id.html" "http://127.0.0.1:$PORT/api/trips/$id/export/html" || true
  done
fi

# rotation: newest KEEP snapshots survive, each with its companions
ls -1 "$OUT"/auto-*.db | sort | head -n -"$KEEP" | while read -r old; do
  base="${old%.db}"; rm -f "$base.db" "$base.db.sha256" "$base-uploads.tgz" "$base"-viaje*.html
  echo "$(date '+%F %R') rotada $(basename "$base")"
done
echo "$(date '+%F %R') guardada auto-$stamp"
