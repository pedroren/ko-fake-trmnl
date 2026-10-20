"""ko-fake-trmnl: a tiny fake TRMNL BYOS server (Flask + JSON file)."""
import json, logging, os, re, sys, tempfile, threading, time
from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

DATA_DIR = os.environ.get("DATA_DIR", "./data")
DATA_FILE = os.path.join(DATA_DIR, "data.json")
IMAGES_DIR = os.path.join(DATA_DIR, "images")
DEFAULT_PLAYLIST = os.environ.get("DEFAULT_PLAYLIST", "default")
DEFAULT_REFRESH = 60
NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

os.makedirs(IMAGES_DIR, exist_ok=True)

# ---------- logging (goes to stdout -> `docker logs` / Portainer) ----------
log = logging.getLogger("ko-fake-trmnl")
log.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_h)
    log.propagate = False
last_seen = {}  # who -> monotonic time of previous request (in memory only)


def parse_battery(raw):
    """Plugin sends 'Percent-Charged: 0-100'. Return an int or None if missing/invalid."""
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return None
    return v if 0 <= v <= 100 else None


def log_serve(who, src, name, idx, total, item):
    now = time.monotonic()
    prev = last_seen.get(who)
    last_seen[who] = now
    gap = f"{now - prev:.1f}s" if prev else "first"
    log.info(
        "serve who=%s src=%s playlist=%s served=%s/%s next=%s refresh=%ss gap=%s url=%s ip=%s ua=%r token=%s battery=%s",
        who, src, name, idx + 1, total, (idx + 1) % total + 1, item.get("refresh_rate"),
        gap, item["image_url"], request.remote_addr, request.headers.get("User-Agent", ""),
        "yes" if request.headers.get("Access-Token") else "no",
        request.headers.get("Percent-Charged", "n/a"),
    )
    log.debug("headers=%s", {k: v for k, v in request.headers if k.lower() != "access-token"})
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
        return None, None
    i = holder.get("cursor", 0) % len(items)
    holder["cursor"] = (i + 1) % len(items)
    return i, items[i]


def trmnl_response(item):
    url = item["image_url"]
    if url.startswith("/"):  # relative -> absolute, so you can use uploaded images
        url = request.host_url.rstrip("/") + url
    resp = jsonify(
        status=0,
        image_url=url,
        filename=os.path.basename(url.split("?")[0]),
        refresh_rate=int(item.get("refresh_rate", DEFAULT_REFRESH)),
    )
    resp.headers["Cache-Control"] = "no-store"  # never let a proxy/client reuse a response
    return resp


def empty_playlist():
    return jsonify(status=404, error="playlist is empty or missing"), 404


# ---------- client endpoints ----------
@app.get("/playlist/<name>")
def playlist(name):
    with lock:
        db = load()
        pl = db["playlists"].get(name)
        idx, item = next_item(pl, pl["items"]) if pl else (None, None)
        if item:
            save(db)
    if item is None:
        log.warning("empty src=/playlist playlist=%s (missing or no items) ip=%s", name, request.remote_addr)
        return empty_playlist()
    log_serve(f"playlist:{name}", "/playlist", name, idx, len(pl["items"]), item)
    return trmnl_response(item)


def serve_device(device_id, src):
    with lock:
        db = load()
        if device_id not in db["devices"]:
            log.info("new device registered: %s", device_id)
        dev = db["devices"].setdefault(device_id, {"playlist": None, "cursor": 0})
        dev["last_seen"] = int(time.time())
        batt = parse_battery(request.headers.get("Percent-Charged"))
        if batt is not None:  # keep the previous reading if this request lacked the header
            dev["battery"] = batt
        name = dev.get("playlist") or DEFAULT_PLAYLIST
        pl = db["playlists"].get(name)
        idx, item = next_item(dev, pl["items"]) if pl else (None, None)
        save(db)  # also persists auto-registered devices
    if item is None:
        log.warning("empty who=%s src=%s playlist=%s (missing or no items)", device_id, src, name)
        return empty_playlist()
    log_serve(device_id, src, name, idx, len(pl["items"]), item)
    return trmnl_response(item)


@app.get("/device/<device_id>")
def device(device_id):
    return serve_device(device_id, "/device")


@app.get("/api/display")  # what real TRMNL clients call
def api_display():
    device_id = request.headers.get("ID") or request.headers.get("Access-Token") or "unknown"
    return serve_device(device_id, "/api/display")


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

@app.get("/favicon.ico")
def index():
    return app.send_static_file("favicon.ico")

@app.get("/admin/api/state")
def admin_state():
    with lock:
        db = load()
    db["now"] = int(time.time())  # lets the UI compute "seen x min ago" without trusting its own clock
    return jsonify(db)


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
    log.info("admin: playlist %s saved with %d items (cursor now %d)", name, len(items), pl["cursor"])
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
    log.info("admin: device %s -> playlist %s (cursor %d)", device_id, pl, dev["cursor"])
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
