# Nucleares Bridge

A lightweight Python service that runs on your gaming PC alongside Nucleares.
It polls the game's local webserver and exposes the data over a LAN REST API
so Home Assistant (or anything else on your network) can read it.

## How it works

```
Nucleares (localhost:8080)
        ↓  poll every N seconds
  bridge.py  (this script)
        ↓  REST API
Home Assistant (192.168.x.x:8123)
```

The bridge never connects to Home Assistant — HA always calls the bridge.
No HA credentials are stored on this machine.

---

## Requirements

- Python 3.11+
- Nucleares running with the webserver activated
  (in-game tablet → Status → Start webserver)

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env`:

| Variable | Description |
|---|---|
| `HA_API_KEY` | A long random secret. Generate with: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `ALLOWED_IP` | IP of your HA server. Leave blank to allow any LAN IP. |
| `BRIDGE_PORT` | Port the bridge listens on (default: `8765`) |
| `POLL_INTERVAL` | Seconds between Nucleares polls (default: `5`) |
| `NUCLEARES_URL` | Nucleares webserver URL (default: `http://localhost:8080/`) |

### 3. Edit variables (optional)

`variables.yaml` lists every Nucleares variable the bridge will poll.
Add or remove entries freely. Variable names must match exactly what
the Nucleares API expects.

### 4. Run (manual)

```bash
python bridge.py
```

---

## Running as a Windows Service (recommended)

Use [NSSM](https://nssm.cc/) so the bridge starts automatically with Windows
and restarts if it crashes.

1. Download NSSM and place `nssm.exe` somewhere on your PATH (e.g. `C:\Windows\`)
2. Open an **administrator** command prompt
3. Run:

```
nssm install NuclearesBridge
```

4. In the GUI that opens:
   - **Path**: full path to your Python exe, e.g. `C:\Python311\python.exe`
   - **Startup directory**: folder containing `bridge.py`
   - **Arguments**: `bridge.py`

5. Under the **Environment** tab, add:

```
PATH=C:\Python311;C:\Python311\Scripts
```

6. Click **Install service**, then:

```
nssm start NuclearesBridge
```

To stop or remove:
```
nssm stop NuclearesBridge
nssm remove NuclearesBridge confirm
```

---

## API Reference

All endpoints require the header: `X-API-Key: <your key>`

### `GET /health`

Returns bridge and game status.

```json
{
  "status": "ok",
  "game_connected": true,
  "last_poll": "2026-03-08T14:32:01+00:00"
}
```

### `GET /sensors`

Returns all polled sensor values.

```json
{
  "game_connected": true,
  "last_poll": "2026-03-08T14:32:01+00:00",
  "sensors": {
    "CORE_TEMP": { "value": 312.4, "value_str": "312.4" },
    "CORE_PRESSURE": { "value": 155.2, "value_str": "155.2" }
  }
}
```

### `GET /sensors/<VARIABLE>`

Returns a single variable.

```json
{ "variable": "CORE_TEMP", "value": 312.4, "value_str": "312.4" }
```

### `POST /control`

Sends a command to the game.

```json
{ "variable": "RODS_ALL_POS_ORDERED", "value": 0 }
```

Response:
```json
{ "success": true, "variable": "RODS_ALL_POS_ORDERED", "value": 0 }
```

---

## Security notes

- The bridge only listens on your LAN — do not port-forward it to the internet
- Set `ALLOWED_IP` to your HA server's IP for the strongest protection
- Keep `.env` out of version control (it is in `.gitignore`)
- The bridge holds no HA credentials
