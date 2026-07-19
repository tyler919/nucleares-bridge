"""
Nucleares Bridge
Polls the Nucleares game webserver (localhost:8080) and exposes the data
over a local REST API so Home Assistant can pull from it.
"""

import collections
import os
import time
import threading
import logging
import logging.handlers
from datetime import datetime, timezone

import yaml
import requests
from flask import Flask, jsonify, request, abort, render_template
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
API_KEY        = os.getenv("HA_API_KEY", "")
ALLOWED_IP     = os.getenv("ALLOWED_IP", "")
BRIDGE_PORT    = int(os.getenv("BRIDGE_PORT", 8765))
POLL_INTERVAL  = int(os.getenv("POLL_INTERVAL", 5))
NUCLEARES_URL  = os.getenv("NUCLEARES_URL", "http://localhost:8080/")
LOG_FILE       = os.getenv("LOG_FILE", "bridge.log")
LOG_LEVEL      = os.getenv("LOG_LEVEL", "INFO").upper()

# ---------------------------------------------------------------------------
# Logging — console + rotating file + in-memory ring buffer for /logs
# ---------------------------------------------------------------------------
_LOG_BUFFER: collections.deque = collections.deque(maxlen=500)
_LOG_FMT = "%(asctime)s [%(levelname)s] %(message)s"
_LOG_DATE = "%H:%M:%S"


class _BufferHandler(logging.Handler):
    """Captures log records into the in-memory ring buffer for the UI."""
    def emit(self, record: logging.LogRecord) -> None:
        _LOG_BUFFER.append({
            "time":    datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
            "level":   record.levelname,
            "message": record.getMessage(),
        })


def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("nucleares")
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    fmt = logging.Formatter(_LOG_FMT, datefmt=_LOG_DATE)

    # Console
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # Rotating file (5 MB × 3 backups)
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # In-memory buffer (no formatter needed — we build the dict manually)
    bh = _BufferHandler()
    bh.setLevel(logging.DEBUG)
    logger.addHandler(bh)

    return logger


log = _setup_logging()

# ---------------------------------------------------------------------------
# Load variable list
# variables.yaml      — user's local copy (gitignored, never overwritten by updates)
# variables.default.yaml — shipped defaults, used if no local copy exists
# ---------------------------------------------------------------------------
_VAR_FILE = "variables.yaml" if os.path.exists("variables.yaml") else "variables.default.yaml"
with open(_VAR_FILE) as f:
    _cfg = yaml.safe_load(f)

VARIABLES: list[dict] = _cfg.get("variables", [])
log.info("Loaded %d variables from %s", len(VARIABLES), _VAR_FILE)

# ---------------------------------------------------------------------------
# Thread-safe state
# ---------------------------------------------------------------------------
_cache_lock      = threading.Lock()
_cache: dict     = {}
_game_connected  = False
_last_poll: str | None = None
_poll_count      = 0          # total successful poll cycles
_error_count     = 0          # total failed poll cycles since start
_prev_connected  = None       # track state changes for log messages


# ---------------------------------------------------------------------------
# Poller thread
# ---------------------------------------------------------------------------
def _poll_loop() -> None:
    global _game_connected, _last_poll, _poll_count, _error_count, _prev_connected

    log.info("Poller started — interval %ds, target %s", POLL_INTERVAL, NUCLEARES_URL)

    while True:
        results: dict = {}
        failed: list  = []

        for var in VARIABLES:
            name = var["name"]
            try:
                r = requests.get(
                    NUCLEARES_URL,
                    params={"Variable": name},
                    timeout=3,
                )

                # 404 = variable doesn't exist in this game version — skip silently
                if r.status_code == 404:
                    log.debug("Variable %s not found (404) — skipping", name)
                    results[name] = {"value": None, "value_str": None}
                    continue

                r.raise_for_status()

                # Empty body = variable has no value right now
                text = r.text.strip()
                if not text:
                    results[name] = {"value": None, "value_str": None}
                    continue

                # Parse response — the game API returns either:
                #   a) A raw value directly: 312.4 / 1 / "ACTIVE"
                #   b) A JSON object: {"value": ..., "value_str": ..., "errors": null}
                try:
                    data = r.json()
                except ValueError:
                    results[name] = {"value": None, "value_str": None}
                    continue

                if isinstance(data, dict):
                    if data.get("errors") is None:
                        results[name] = {
                            "value":     data.get("value"),
                            "value_str": data.get("value_str"),
                        }
                    else:
                        results[name] = {"value": None, "value_str": None}
                        failed.append(name)
                else:
                    # Raw value — wrap it consistently
                    results[name] = {
                        "value":     data,
                        "value_str": str(data),
                    }

            except requests.ConnectionError:
                results[name] = {"value": None, "value_str": None}
                failed.append(name)
            except requests.Timeout:
                log.warning("Timeout polling %s", name)
                results[name] = {"value": None, "value_str": None}
                failed.append(name)
            except Exception as exc:
                log.warning("Unexpected error polling %s: %s", name, exc)
                results[name] = {"value": None, "value_str": None}
                failed.append(name)

        all_ok = len(failed) == 0

        with _cache_lock:
            _cache.update(results)
            _game_connected = all_ok
            _last_poll = datetime.now(timezone.utc).isoformat()

            if all_ok:
                _poll_count += 1
            else:
                _error_count += 1

        # Log connection state changes
        if _prev_connected is None or _prev_connected != all_ok:
            if all_ok:
                log.info(
                    "Game connection established — polling %d variables",
                    len(VARIABLES),
                )
            else:
                log.warning(
                    "Game connection lost — %d/%d variables failed (e.g. %s)",
                    len(failed),
                    len(VARIABLES),
                    failed[0] if failed else "?",
                )
            _prev_connected = all_ok

        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------
_LOOPBACK = {"127.0.0.1", "::1"}


def _check_auth() -> None:
    """Abort request if API key or source IP is wrong."""
    if API_KEY:
        provided = request.headers.get("X-API-Key", "")
        if provided != API_KEY:
            log.warning(
                "Rejected %s %s from %s — bad API key",
                request.method, request.path, request.remote_addr,
            )
            abort(401, description="Invalid API key.")

    if ALLOWED_IP:
        allowed = {ALLOWED_IP} | _LOOPBACK
        if request.remote_addr not in allowed:
            log.warning(
                "Rejected %s %s from %s — IP not in allowlist (allowed: %s)",
                request.method, request.path, request.remote_addr, ALLOWED_IP,
            )
            abort(403, description="Forbidden.")


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.errorhandler(400)
@app.errorhandler(401)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(502)
def _error_handler(exc):
    return jsonify({"error": str(exc)}), exc.code


# GET /health
@app.route("/health")
def health():
    _check_auth()
    with _cache_lock:
        return jsonify({
            "status":         "ok",
            "game_connected": _game_connected,
            "last_poll":      _last_poll,
            "poll_count":     _poll_count,
            "error_count":    _error_count,
            "variable_count": len(VARIABLES),
        })


# GET /sensors  — all variables
@app.route("/sensors")
def sensors():
    _check_auth()
    log.debug("GET /sensors from %s", request.remote_addr)
    with _cache_lock:
        return jsonify({
            "game_connected": _game_connected,
            "last_poll":      _last_poll,
            "sensors":        dict(_cache),
        })


# GET /sensors/<VARIABLE_NAME>  — single variable
@app.route("/sensors/<variable>")
def sensor_single(variable: str):
    _check_auth()
    key = variable.upper()
    with _cache_lock:
        if key not in _cache:
            abort(404, description=f"Variable '{key}' not in poll list.")
        return jsonify({"variable": key, **_cache[key]})


# POST /control  — send a command to the game
@app.route("/control", methods=["POST"])
def control():
    _check_auth()

    body = request.get_json(silent=True)
    if not body or "variable" not in body or "value" not in body:
        abort(400, description="Body must contain 'variable' and 'value'.")

    variable = str(body["variable"]).upper()
    value    = body["value"]

    log.info(
        "Control command from %s: %s = %s",
        request.remote_addr, variable, value,
    )

    # The Nucleares webserver accepts writes as a POST whose Variable and Value
    # ride in the query string (a form-body POST is rejected). A valid write
    # returns HTTP 200; an unknown writable name returns 404 with a message like
    # "The writable variable 'X' does not exist."
    from urllib.parse import urlsplit, quote
    parts = urlsplit(NUCLEARES_URL)
    root  = f"{parts.scheme}://{parts.netloc}"
    url   = f"{root}/?Variable={quote(variable)}&Value={quote(str(value))}"

    try:
        r = requests.post(url, timeout=3)
        game_text = r.text.strip()
        if r.status_code == 200:
            log.info("Control command accepted by game: %s = %s", variable, value)
            return jsonify({"success": True, "variable": variable,
                            "value": value, "game_response": game_text})
        log.warning("Game rejected %s = %s (HTTP %d): %s",
                    variable, value, r.status_code, game_text)
        return jsonify({"success": False, "variable": variable, "value": value,
                        "status": r.status_code, "game_response": game_text}), 502

    except requests.RequestException as exc:
        log.error("Control command failed — could not reach game: %s", exc)
        return jsonify({"success": False, "error": str(exc)}), 502


# GET /logs  — recent log entries (requires API key)
@app.route("/logs")
def logs():
    _check_auth()
    level  = request.args.get("level", "").upper()
    limit  = min(int(request.args.get("limit", 200)), 500)

    entries = list(_LOG_BUFFER)

    if level:
        entries = [e for e in entries if e["level"] == level]

    return jsonify({
        "total":   len(_LOG_BUFFER),
        "entries": entries[-limit:],
    })


# POST /rawtest  — TEMPORARY: send an arbitrary HTTP request to the game so the
# correct write format can be discovered. Protected by the API key. Remove once
# /control is fixed.
@app.route("/rawtest", methods=["POST"])
def rawtest():
    _check_auth()
    from urllib.parse import urlsplit
    b = request.get_json(silent=True) or {}
    method  = (b.get("method") or "GET").upper()
    path    = b.get("path", "/")
    body    = b.get("body")               # raw string or None
    headers = b.get("headers") or {}

    parts = urlsplit(NUCLEARES_URL)
    root  = f"{parts.scheme}://{parts.netloc}"
    url   = root + path

    kwargs = {"timeout": 4, "headers": headers}
    if body is not None:
        kwargs["data"] = body.encode() if isinstance(body, str) else body

    try:
        r = requests.request(method, url, **kwargs)
        return jsonify({
            "sent":   {"method": method, "url": url, "body": body, "headers": headers},
            "status": r.status_code,
            "text":   r.text[:2000],
        })
    except Exception as exc:
        return jsonify({"sent": {"method": method, "url": url, "body": body},
                        "error": str(exc)}), 200


# GET /gamefiles  — TEMPORARY: locate the Nucleares install and return its
# XMLScript definition files, which enumerate the writable variables for this
# game version. Protected by the API key. Remove once mapping is done.
@app.route("/gamefiles")
def gamefiles():
    _check_auth()
    import re as _re

    steam_roots = []
    try:
        import winreg
        for hive, key, valname in [
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        ]:
            try:
                k = winreg.OpenKey(hive, key)
                val, _ = winreg.QueryValueEx(k, valname)
                if val:
                    steam_roots.append(val)
            except OSError:
                pass
    except Exception:
        pass

    import string
    for d in string.ascii_uppercase:
        for p in (f"{d}:\\Program Files (x86)\\Steam", f"{d}:\\Steam", f"{d}:\\SteamLibrary"):
            if os.path.isdir(p):
                steam_roots.append(p)

    libs = set()
    for sr in steam_roots:
        libs.add(sr)
        vdf = os.path.join(sr, "steamapps", "libraryfolders.vdf")
        if os.path.isfile(vdf):
            try:
                txt = open(vdf, encoding="utf-8", errors="replace").read()
                for m in _re.finditer(r'"path"\s*"([^"]+)"', txt):
                    libs.add(m.group(1).replace("\\\\", "\\"))
            except Exception:
                pass

    game_dir = xmldir = None
    for lib in libs:
        common = os.path.join(lib, "steamapps", "common")
        if not os.path.isdir(common):
            continue
        try:
            for gf in os.listdir(common):
                if "nuclear" in gf.lower():
                    gd = os.path.join(common, gf)
                    cand = os.path.join(gd, "Assets", "XMLScript")
                    game_dir = gd
                    if os.path.isdir(cand):
                        xmldir = cand
                        break
        except Exception:
            pass
        if xmldir:
            break

    if not xmldir:
        return jsonify({"found": False, "searched_libraries": sorted(libs),
                        "game_dir": game_dir})

    want = request.args.get("file")
    listing = sorted(os.listdir(xmldir))
    if want:
        fp = os.path.join(xmldir, os.path.basename(want))
        if not os.path.isfile(fp):
            abort(404, description=f"{want} not found in XMLScript.")
        return jsonify({"file": want,
                        "content": open(fp, encoding="utf-8", errors="replace").read()[:600000]})

    out = {"found": True, "xmldir": xmldir, "files": listing}
    # auto-include any patch-note text file (documents settable vars)
    for fn in listing:
        if "patch" in fn.lower() and fn.lower().endswith(".txt"):
            try:
                out["patch_note_name"] = fn
                out["patch_note"] = open(os.path.join(xmldir, fn),
                                         encoding="utf-8", errors="replace").read()[:600000]
            except Exception as e:
                out["patch_note"] = f"<read error: {e}>"
            break
    return jsonify(out)


# ---------------------------------------------------------------------------
# UI routes — no API key required, accessible from any browser on the LAN
# ---------------------------------------------------------------------------

@app.route("/ui")
def ui():
    return render_template("ui.html")


@app.route("/ui/data")
def ui_data():
    # UI routes are open to any browser on the LAN — no API key or IP restriction.
    # They only expose game telemetry, not controls or credentials.
    with _cache_lock:
        return jsonify({
            "game_connected": _game_connected,
            "last_poll":      _last_poll,
            "poll_count":     _poll_count,
            "error_count":    _error_count,
            "sensors":        dict(_cache),
        })


@app.route("/ui/logs")
def ui_logs():
    level  = request.args.get("level", "").upper()
    limit  = min(int(request.args.get("limit", 200)), 500)
    entries = list(_LOG_BUFFER)
    if level:
        entries = [e for e in entries if e["level"] == level]
    return jsonify({
        "total":   len(_LOG_BUFFER),
        "entries": entries[-limit:],
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    t = threading.Thread(target=_poll_loop, daemon=True, name="nucleares-poller")
    t.start()

    log.info(
        "Bridge listening on 0.0.0.0:%d — UI at http://localhost:%d/ui",
        BRIDGE_PORT, BRIDGE_PORT,
    )
    app.run(host="0.0.0.0", port=BRIDGE_PORT)
