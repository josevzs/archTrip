"""Photos added by hand: an http(s) URL, or a file that is stored resized next to the DB."""
import io
import re
import uuid
from pathlib import Path

from flask import current_app
from PIL import Image, ImageOps

LARGE = 1600
THUMB = 640
URL_RE = re.compile(r"^https?://", re.I)


def is_http_url(u):
    return bool(u) and bool(URL_RE.match(u.strip()))


def store_file(landmark_id, data):
    """Save two JPEG copies (large + thumb) of an uploaded image.
    -> (url, thumb) as site-relative paths. Raises ValueError if it isn't an image."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:
        raise ValueError("El archivo no es una imagen") from exc
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    folder = Path(current_app.config["UPLOAD_DIR"]) / str(landmark_id)
    folder.mkdir(parents=True, exist_ok=True)
    stem = uuid.uuid4().hex
    out = {}
    for suffix, size in (("", LARGE), ("_thumb", THUMB)):
        copy = img.copy()
        copy.thumbnail((size, size))
        name = f"{stem}{suffix}.jpg"
        copy.save(folder / name, "JPEG", quality=85, optimize=True)
        out[suffix] = f"/uploads/{landmark_id}/{name}"
    return out[""], out["_thumb"]


def delete_files(image):
    """Remove the stored copies of a manually uploaded image (no-op for URLs)."""
    base = Path(current_app.config["UPLOAD_DIR"])
    for key in ("url", "thumb"):
        u = image.get(key) or ""
        if u.startswith("/uploads/"):
            path = base / u[len("/uploads/"):]
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def read_file(url):
    """Bytes of a stored upload by its site-relative URL, or None."""
    if not url.startswith("/uploads/"):
        return None
    path = Path(current_app.config["UPLOAD_DIR"]) / url[len("/uploads/"):]
    return path.read_bytes() if path.is_file() else None
