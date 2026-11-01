# El buzón de fotos de los alumnos

Un Worker de Cloudflare donde los alumnos suben, desde el móvil, las fotos que hacen durante el
viaje. Cada foto va firmada (nombre obligatorio), atada a una obra del viaje y entra **pendiente**:
no se publica nada hasta que el profesor la aprueba.

El buzón no es el archivo. Guarda lo justo hasta que panda pasa a recogerlo (cada minuto, con
`archtrip/inbox.py`): panda se lleva cada foto, la guarda en su disco y la borra de aquí. **Panda
nunca recibe conexiones**: todo lo que va de aquí a allí lo pide panda. Por eso el cubo no
acumula, y por eso su tope de 5 GB no es un freno de gasto sino una alarma de que la recogida lleva
días parada.

## Piezas

| Archivo | Qué es |
|---|---|
| `worker.js` | el Worker entero: formulario, subida, y las rutas de panda. Un solo archivo, sin dependencias. |
| `wrangler.toml` | nombre, enlaces a R2 (`FOTOS`) y D1 (`BUZON`). Sin secretos. |
| `esquema.sql` | las tablas de D1: `submissions`, `quotas`, `catalog`. |
| `prueba.mjs` | pruebas sin red: `node --test buzon/prueba.mjs` (o `npm test` dentro de `buzon/`). Con Node 22 prueban también el Worker entero contra SQLite de verdad. |
| `package.json` | solo marca el código como módulo ES y define `npm test`. No hay que instalar nada. |

## Rutas

Públicas (el alumno):

| Ruta | Qué hace |
|---|---|
| `GET /<viaje>` | el formulario de subida. `<viaje>` es el nombre del proyecto publicado en Pages (p. ej. `japon-2026`). Sin catálogo, 404. |
| `GET /<viaje>/obras` | la lista de obras del viaje (JSON). |
| `POST /<viaje>/subir` | multipart con `author`, `landmark_id` y **un solo** archivo `photo`. |

De panda, con `Authorization: Bearer <BUZON_SECRET>`:

| Ruta | Qué hace |
|---|---|
| `GET /panda/pendientes` | los envíos sin recoger (hasta 200), sin los bytes. |
| `GET /panda/objeto/<clave>` | los bytes de una foto. |
| `POST /panda/recogido` | `{"ids": [...]}`: borra esas fotos de R2 y las marca recogidas. |
| `PUT /panda/catalogo/<viaje>` | panda deja aquí la lista de obras (`{"trip_name", "landmarks": [{id, name, architect, city, lat, lon, day}]}`). |
| `GET /panda/estado` | envíos sin recoger, bytes en el cubo, cuenta del día. |

## Qué comprueba al subir, y en este orden

Falta el nombre → falta la obra → la obra no está en el catálogo → no hay archivo o hay más de uno
→ no es una imagen (por los primeros bytes: JPEG, PNG o WebP; HEIC se rechaza con un mensaje que
explica cómo cambiarlo en el iPhone) → pasa de 25 MB → esta conexión ya ha subido 40 hoy → el cubo
pasa de 5 GB. Los mensajes están en castellano y el formulario los enseña tal cual.

Se sube el original, sin encoger. La IP no se guarda nunca: solo `sha256(ip + BUZON_SECRET)`, para
la cuenta diaria. Si la foto trae GPS en el EXIF, el formulario ordena la lista por cercanía y marca
la más próxima, pero la obra la elige siempre el alumno.

## Desplegar (una vez)

Hace falta una cuenta de Cloudflare con permiso para Workers, R2 y D1 (`npx wrangler login`, o un
token con *Workers Scripts: Edit*, *Workers R2 Storage: Edit* y *D1: Edit*). El token que usa panda
para publicar las webs (*Cloudflare Pages: Edit*) **no** basta para esto, y panda no lo necesita.

```
cd buzon
npx wrangler r2 bucket create archtrip-fotos
npx wrangler d1 create archtrip-buzon        # copia el database_id que devuelve a wrangler.toml
npx wrangler d1 execute archtrip-buzon --remote --file=esquema.sql
npx wrangler secret put BUZON_SECRET         # una cadena larga y aleatoria, p. ej.:
                                             #   python -c "import secrets; print(secrets.token_urlsafe(32))"
npx wrangler deploy                          # dice la dirección: https://archtrip-buzon.<cuenta>.workers.dev
```

Comprobación: `curl -H "Authorization: Bearer <BUZON_SECRET>" https://archtrip-buzon.<cuenta>.workers.dev/panda/estado`
devuelve `{"nuevos":0,...}`.

Después, en panda (`.env` junto a `docker-compose.yml`):

```
ARCHTRIP_INBOX_URL=https://archtrip-buzon.<cuenta>.workers.dev
ARCHTRIP_INBOX_SECRET=<el mismo BUZON_SECRET>
```

y `docker compose up -d --build`. En el primer minuto panda sube el catálogo de cada viaje
publicado y el formulario queda en `https://archtrip-buzon.<cuenta>.workers.dev/<viaje>`.

## Variables

| Dónde | Nombre | Qué es |
|---|---|---|
| Worker (secreto) | `BUZON_SECRET` | la llave de las rutas `/panda/` y la sal del hash de las IP. |
| Worker (enlace) | `FOTOS` | el cubo R2 `archtrip-fotos`. |
| Worker (enlace) | `BUZON` | la base D1 `archtrip-buzon`. |
| panda | `ARCHTRIP_INBOX_URL`, `ARCHTRIP_INBOX_SECRET` | dónde está el buzón y su llave. |

Los topes (25 MB por foto, 40 al día por conexión, 5 GB en el cubo) son constantes al principio de
`worker.js`.
