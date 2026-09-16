import os
from pathlib import Path

from flask import Flask, send_from_directory

from . import db as _db

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"


def create_app(db_path=None):
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    app.config["DB_PATH"] = str(
        db_path or os.environ.get("ARCHTRIP_DB") or (ROOT / "data" / "archtrip.db")
    )
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
    # photos the professors upload by hand live next to the database (bind-mounted in Docker)
    app.config["UPLOAD_DIR"] = str(Path(app.config["DB_PATH"]).parent / "uploads")
    app.json.ensure_ascii = False

    _db.init_app(app)

    from .routes import api

    app.register_blueprint(api, url_prefix="/api")

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/uploads/<path:name>")
    def uploads(name):
        return send_from_directory(app.config["UPLOAD_DIR"], name, max_age=86400)

    return app
