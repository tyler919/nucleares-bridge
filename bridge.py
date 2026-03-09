"""
Nucleares Bridge
Polls the Nucleares game webserver (localhost:8080) and exposes the data
over a local REST API so Home Assistant can pull from it.
"""

import os
import time
import threading
import logging
from datetime import datetime, timezone

import yaml
import requests
from flask import Flask, jsonify, request, abort
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
API_KEY        = os.getenv("HA_API_KEY", "")
ALLOWED_IP     = os.getenv("ALLOWED_IP", "")        # empty = any LAN IP allowed
BRIDGE_PORT    = int(os.getenv("BRIDGE_PORT", 8765))
POLL_INTERVAL  = int(os.getenv("POLL_INTERVAL", 5))
NUCLEARES_URL  = os.getenv("NUCLEARES_URL", "http://localhost:8080/")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Load variable list
# ---------------------------------------------------------------------------
with open("variables.yaml") as f:
    _cfg = yaml.safe_load(f)

VARIABLES: list[dict] = _cfg.get("variables", [])

# ---------------------------------------------------------------------------
# Thread-safe cache
# ---------------------------------------------------------------------------
_cache_lock    = threading.Lock()
_cache: dict   = {}
_game_connected = False
_last_poll: str | None = None


# ---------------------------------------------------------------------------
# Poller thread
# ---------------------------------------------------------------------------
def _poll_loop() -> None:
    global _game_connected, _last_poll

    log.info("Poller started — interval %ds", POLL_INTERVAL)

    while True:
        results: dict = {}
        all_ok = True

        for var in VARIABLES:
            name = var["name"]
            try:
                r = requests.get(
                    NUCLEARES_URL,
                    params={"Variable": name},
                    timeout=3,
                )
                r.raise_for_status()
                data = r.json()

                if data.get("errors") is None:
                    results[name] = {
                        "value":     data.get("value"),
                        "value_str": data.get("value_str"),
                    }
                else:
                    results[name] = {"value": None, "value_str": None}
                    all_ok = False

            except Exception as exc:
                log.debug("Poll failed for %s: %s", name, exc)
                results[name] = {"value": None, "value_str": None}
                all_ok = False

        with _cache_lock:
            _cache.update(results)
            _game_connected = all_ok
            _last_poll = datetime.now(timezone.utc).isoformat()

        if not all_ok:
            log.warning("Game unreachable or some variables failed — retrying in %ds", POLL_INTERVAL)

        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------
def _check_auth() -> None:
    """Abort request if API key or source IP is wrong."""
    if API_KEY:
        provided = request.headers.get("X-API-Key", "")
        if provided != API_KEY:
            log.warning("Rejected request from %s — bad API key", request.remote_addr)
            abort(401, description="Invalid API key.")

    if ALLOWED_IP:
        if request.remote_addr != ALLOWED_IP:
            log.warning("Rejected request from %s — IP not allowed", request.remote_addr)
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
        })


# GET /sensors  — all variables
@app.route("/sensors")
def sensors():
    _check_auth()
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
        return jsonify({
            "variable":  key,
            **_cache[key],
        })


# POST /control  — send a command to the game
@app.route("/control", methods=["POST"])
def control():
    _check_auth()

    body = request.get_json(silent=True)
    if not body or "variable" not in body or "value" not in body:
        abort(400, description="Body must contain 'variable' and 'value'.")

    variable = str(body["variable"]).upper()
    value    = body["value"]

    log.info("Control command: %s = %s", variable, value)

    try:
        r = requests.post(
            NUCLEARES_URL,
            data={"Variable": variable, "Value": value},
            timeout=3,
        )
        r.raise_for_status()
        return jsonify({"success": True, "variable": variable, "value": value})

    except requests.RequestException as exc:
        log.error("Failed to send control command to game: %s", exc)
        return jsonify({"success": False, "error": str(exc)}), 502


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Start background poller
    t = threading.Thread(target=_poll_loop, daemon=True, name="nucleares-poller")
    t.start()

    log.info("Bridge listening on 0.0.0.0:%d", BRIDGE_PORT)
    app.run(host="0.0.0.0", port=BRIDGE_PORT)
