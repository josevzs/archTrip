FROM python:3.12-slim

WORKDIR /app
# el itinerario en PDF necesita una monoespaciada con tildes y macrones (Hōryū-ji)
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
# Node.js y wrangler, con los que el servidor sube a Cloudflare Pages la versión de los alumnos
# (archtrip/publish.py). wrangler 4.152 pide Node 22 o más reciente.
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && npm install -g wrangler@4.152.0 && npm cache clean --force
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY app.py .
COPY archtrip/ archtrip/
COPY static/ static/

ENV ARCHTRIP_DB=/app/data/archtrip.db
VOLUME ["/app/data"]
EXPOSE 8000

# One worker on purpose: the Nominatim rate limiter is an in-process lock, and the thread that
# publishes to Cloudflare must run only once.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "--timeout", "60", "app:app"]
