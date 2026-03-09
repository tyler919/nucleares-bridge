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
        if request.remote_addr != ALLOWED_IP:
            log.warning(
                "Rejected %s %s from %s — IP not in allowlist",
                request.method, request.path, request.remote_addr,
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

    try:
        r = requests.post(
            NUCLEARES_URL,
            data={"Variable": variable, "Value": value},
            timeout=3,
        )
        r.raise_for_status()
        log.info("Control command accepted by game: %s = %s", variable, value)
        return jsonify({"success": True, "variable": variable, "value": value})

    except requests.RequestException as exc:
        log.error("Control command failed — game rejected it: %s", exc)
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


# ---------------------------------------------------------------------------
# UI routes — no API key required, accessible from any browser on the LAN
# ---------------------------------------------------------------------------

@app.route("/ui")
def ui():
    return render_template("ui.html")


@app.route("/ui/data")
def ui_data():
    if ALLOWED_IP and request.remote_addr not in (ALLOWED_IP, "127.0.0.1"):
        abort(403, description="Forbidden.")
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
    if ALLOWED_IP and request.remote_addr not in (ALLOWED_IP, "127.0.0.1"):
        abort(403, description="Forbidden.")
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
