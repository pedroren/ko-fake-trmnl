"""ko-fake-trmnl: a tiny fake TRMNL BYOS server (Flask + JSON file)."""
import json, os, re, tempfile, threading
from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

DATA_DIR = os.environ.get("DATA_DIR", "./data")
DATA_FILE = os.path.join(DATA_DIR, "data.json")
IMAGES_DIR = os.path.join(DATA_DIR, "images")
DEFAULT_PLAYLIST = os.environ.get("DEFAULT_PLAYLIST", "default")
DEFAULT_REFRESH = 60
NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

os.makedirs(IMAGES_DIR, exist_ok=True)
app = Flask(__name__, static_folder="static", static_url_path="/static")
lock = threading.RLock()  # run with ONE gunicorn worker (threads are fine)


# ---------- persistence ----------
def load():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            db = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        db = {}
    db.setdefault("playlists", {})
    db.setdefault("devices", {})
    return db


def save(db):
    """Atomic write: temp file + rename, so a crash never corrupts the JSON."""
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, DATA_FILE)


# ---------- playlist logic ----------
def next_item(holder, items):
    """Return the current item and advance the cursor (holder = playlist or device)."""
    if not items:
        return None
    i = holder.get("cursor", 0) % len(items)
    holder["cursor"] = (i + 1) % len(items)
    return items[i]


def trmnl_response(item):
    url = item["image_url"]
    if url.startswith("/"):  # relative -> absolute, so you can use uploaded images
        url = request.host_url.rstrip("/") + url
    return jsonify(
        status=0,
        image_url=url,
        filename=os.path.basename(url.split("?")[0]),
        refresh_rate=int(item.get("refresh_rate", DEFAULT_REFRESH)),
    )


def empty_playlist():
    return jsonify(status=404, error="playlist is empty or missing"), 404


# ---------- client endpoints ----------
@app.get("/playlist/<name>")
def playlist(name):
    with lock:
        db = load()
        pl = db["playlists"].get(name)
        item = next_item(pl, pl["items"]) if pl else None
        if item:
            save(db)
    return trmnl_response(item) if item else empty_playlist()


def serve_device(device_id):
    with lock:
        db = load()
        dev = db["devices"].setdefault(device_id, {"playlist": None, "cursor": 0})
        pl = db["playlists"].get(dev.get("playlist") or DEFAULT_PLAYLIST)
        item = next_item(dev, pl["items"]) if pl else None
        save(db)  # also persists auto-registered devices
    return trmnl_response(item) if item else empty_playlist()


@app.get("/device/<device_id>")
def device(device_id):
    return serve_device(device_id)


@app.get("/api/display")  # what real TRMNL clients call
def api_display():
    device_id = request.headers.get("ID") or request.headers.get("Access-Token") or "unknown"
    return serve_device(device_id)


@app.post("/api/log")
def api_log():
    return jsonify(status=0)


@app.get("/images/<path:filename>")
def images(filename):
    return send_from_directory(IMAGES_DIR, filename)


# ---------- admin API ----------
def clean_items(raw):
    items = []
    for it in raw or []:
        url = str(it.get("image_url", "")).strip()
        if not url:
            continue
        try:
            rate = max(1, int(it.get("refresh_rate", DEFAULT_REFRESH)))
        except (TypeError, ValueError):
            rate = DEFAULT_REFRESH
        items.append({"image_url": url, "refresh_rate": rate})
    return items


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/admin/api/state")
def admin_state():
    with lock:
        return jsonify(load())


@app.put("/admin/api/playlists/<name>")
def put_playlist(name):
    if not NAME_RE.match(name):
        return jsonify(error="invalid name"), 400
    items = clean_items((request.get_json(silent=True) or {}).get("items"))
    with lock:
        db = load()
        pl = db["playlists"].setdefault(name, {"items": [], "cursor": 0})
        pl["items"] = items
        pl["cursor"] = pl.get("cursor", 0) % len(items) if items else 0
        save(db)
    return jsonify(ok=True)


@app.delete("/admin/api/playlists/<name>")
def del_playlist(name):
    with lock:
        db = load()
        db["playlists"].pop(name, None)
        for d in db["devices"].values():
            if d.get("playlist") == name:
                d["playlist"] = None
        save(db)
    return jsonify(ok=True)


@app.put("/admin/api/devices/<device_id>")
def put_device(device_id):
    if not NAME_RE.match(device_id):
        return jsonify(error="invalid id"), 400
    pl = (request.get_json(silent=True) or {}).get("playlist") or None
    with lock:
        db = load()
        dev = db["devices"].setdefault(device_id, {"cursor": 0})
        if dev.get("playlist") != pl:
            dev["cursor"] = 0
        dev["playlist"] = pl
        save(db)
    return jsonify(ok=True)


@app.delete("/admin/api/devices/<device_id>")
def del_device(device_id):
    with lock:
        db = load()
        db["devices"].pop(device_id, None)
        save(db)
    return jsonify(ok=True)


@app.post("/admin/api/images")
def upload_image():
    f = request.files.get("file")
    name = secure_filename(f.filename) if f else ""
    if not name:
        return jsonify(error="no file"), 400
    f.save(os.path.join(IMAGES_DIR, name))
    return jsonify(url=f"/images/{name}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
