#!/bin/bash
# Publica en Cloudflare Pages la versión de consulta de un viaje (solo lectura, para los alumnos),
# y solo si ha cambiado algo desde la última vez.
#   - la página es la copia HTML autónoma con ?alumnos=1: sin edición, sin descartados, sin lo
#     que está por mirar y sin la gestión de las visitas (ver student_payload en export.py);
#   - al lado van los dos PDF del itinerario (día a día y con fotos), con botón en la página;
#   - se compara un hash de la página sin la fecha de exportación: si no ha cambiado, no se sube.
# La configuración va en scripts/publicar.env (no entra en git; ver publicar.env.example).
# Un viaje «solo en local» no se publica nunca.
# Con cron, cada cuarto de hora:
#   */15 * * * * /srv/apps/archtrip/scripts/publicar.sh >> /srv/apps/archtrip/data/publicar.log 2>&1
# Para forzar una subida aunque no haya cambios: FORZAR=1 scripts/publicar.sh
set -euo pipefail
APP="$(cd "$(dirname "$0")/.." && pwd)"
CONF="${PUBLICAR_CONF:-$APP/scripts/publicar.env}"
[ -f "$CONF" ] || { echo "$(date '+%F %R') falta $CONF (copia publicar.env.example)"; exit 1; }
# shellcheck disable=SC1090
source "$CONF"
: "${VIAJE:?falta VIAJE en $CONF}" "${PROYECTO:?falta PROYECTO en $CONF}"
: "${CLOUDFLARE_API_TOKEN:?falta CLOUDFLARE_API_TOKEN en $CONF}" "${CLOUDFLARE_ACCOUNT_ID:?falta CLOUDFLARE_ACCOUNT_ID en $CONF}"
export CLOUDFLARE_API_TOKEN CLOUDFLARE_ACCOUNT_ID
PORT="${ARCHTRIP_PORT:-8000}"
WRANGLER="${WRANGLER:-npx --yes wrangler@4}"
API="http://127.0.0.1:$PORT/api/trips/$VIAJE"
STATE="$APP/data/publicar"; mkdir -p "$STATE"
log() { echo "$(date '+%F %R') [$PROYECTO] $*"; }

curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null || { log "archTrip no responde en el puerto $PORT"; exit 1; }
private=$(curl -sf "$API" | python3 -c 'import json,sys; print(json.load(sys.stdin)["trip"]["private"])') \
  || { log "no existe el viaje $VIAJE"; exit 1; }
[ "$private" = "0" ] || { log "el viaje $VIAJE está «solo en local»: no se publica"; exit 0; }

work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
site="$work/site"; mkdir -p "$site"
curl -sf -o "$site/index.html" "$API/export/html?alumnos=1&pdfs=1"
new=$(sed 's/"exported_at": "[^"]*"//' "$site/index.html" | sha256sum | cut -d' ' -f1)
last="$STATE/$PROYECTO.sha256"
if [ -z "${FORZAR:-}" ] && [ -f "$last" ] && [ "$(cat "$last")" = "$new" ]; then
  exit 0                                   # sin cambios: ni siquiera se apunta en el log
fi

curl -sf -o "$site/itinerario.pdf" "$API/export/itinerario?alumnos=1"
# las fotos del PDF se bajan de una en una (y se quedan en caché para la próxima vez)
for _ in $(seq 1 2000); do
  fin=$(curl -sf -X POST "$API/export/itinerario/fotos/next" | python3 -c 'import json,sys; print(json.load(sys.stdin)["done"])') || break
  [ "$fin" = "True" ] && break
done
curl -sf -o "$site/itinerario-fotos.pdf" "$API/export/itinerario?alumnos=1&fotos=1" || log "no ha salido el PDF con fotos; se publica sin él"
# Cloudflare Pages no acepta archivos de más de 25 MiB
for f in "$site"/*; do
  [ "$(stat -c %s "$f")" -lt 26214400 ] || { log "$(basename "$f") pasa de 25 MiB: Cloudflare no lo admite"; exit 1; }
done
# que los buscadores no lo indexen (la página ya lo pide, esto cubre también los PDF)
printf '/*\n  X-Robots-Tag: noindex, nofollow\n' > "$site/_headers"

# desde fuera del repositorio, para que wrangler no mezcle esto con el git de archTrip; la primera
# vez se crea el proyecto en Cloudflare (si ya existe, ese paso falla sin más y se sigue)
[ -f "$last" ] || (cd "$work" && $WRANGLER pages project create "$PROYECTO" --production-branch main) >/dev/null 2>&1 || true
(cd "$work" && $WRANGLER pages deploy site --project-name "$PROYECTO" --branch main --commit-dirty=true) \
  > "$work/wrangler.log" 2>&1 || { log "falló la subida:"; cat "$work/wrangler.log"; exit 1; }
echo "$new" > "$last"
log "publicado en https://$PROYECTO.pages.dev"
