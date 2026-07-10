# VitalCast MCP Server

Apple Watch health data bridge. Receives vitals from the VitalCast iOS app via
HTTP and exposes them as MCP tools for the Akashic Agent.

## Architecture

```
VitalCast iOS App
       │
       │ HTTPS POST
       ▼
┌─────────────────────────────────────┐
│      vitalcast_mcp.py               │
│  ┌───────────────────────────────┐  │
│  │ HTTP server (thread, :8321)   │  │
│  │  POST /api/vitals             │  │
│  │  POST /api/alert              │  │
│  │  GET  /health                 │  │
│  └──────────┬────────────────────┘  │
│             │ writes                │
│     ┌───────▼────────┐              │
│     │ vitals.json    │              │
│     │ alerts.json    │              │
│     └───────▲────────┘              │
│             │ reads                 │
│  ┌──────────┴────────────────────┐  │
│  │ MCP stdio (JSON-RPC)          │  │
│  │  get_vitals                   │  │
│  │  get_alerts                   │  │
│  │  get_vital                    │  │
│  └──────────┬────────────────────┘  │
└─────────────┼───────────────────────┘
              │ stdio
              ▼
     Akashic Agent
```

## Prerequisites

- Python 3.12+
- `mcp>=1.0.0` — official Python MCP SDK

## Install

```bash
cd vitalcast/
pip install mcp>=1.0.0
```

Or using `uv`:

```bash
cd vitalcast/
uv pip install mcp>=1.0.0
```

## Usage

### Start the server

```bash
python vitalcast_mcp.py
```

With custom configuration:

```bash
python vitalcast_mcp.py --port 8321 --data-dir /data/vitalcast
```

### Configuration

Via environment variables (or `--flag` equivalents):

| Variable | Default | Description |
|---|---|---|
| `VITALCAST_HOST` | `0.0.0.0` | HTTP bind address |
| `VITALCAST_PORT` | `8321` | HTTP port |
| `VITALCAST_DATA_DIR` | `~/.vitalcast` | Data directory (vitals.json + alerts.json) |
| `VITALCAST_MAX_ALERTS` | `100` | Max queued alerts |

### Register with Akashic Agent

```json
{
  "servers": {
    "vitalcast": {
      "command": ["python", "/path/to/vitalcast/vitalcast_mcp.py", "--port", "8321"],
      "env": {},
      "cwd": "/path/to/vitalcast"
    }
  }
}
```

Or via in-chat command:

```
/mcp_add vitalcast python /path/to/vitalcast/vitalcast_mcp.py --port 8321
```

### Send test data

```bash
# Post vitals
curl -X POST http://localhost:8321/api/vitals \
  -H "Content-Type: application/json" \
  -d '[{"type":"heartRate","value":72,"unit":"count/min","date":"2026-07-11T10:30:00Z"}]'

# Post alert
curl -X POST http://localhost:8321/api/alert \
  -H "Content-Type: application/json" \
  -d '{"type":"high_heart_rate","value":125,"unit":"count/min","timestamp":"2026-07-11T10:30:05Z","message":"Heart rate is 125 bpm"}'

# Health check
curl http://localhost:8321/health
```

## MCP Tools

| Tool | Input | Output |
|---|---|---|
| `get_vitals` | — | Latest snapshot of all health metrics |
| `get_alerts` | — | Pending alerts (clears queue after read) |
| `get_vital` | `type` (string) | Single metric by type |

## Data Storage

- `vitals.json` — overwritten on each `POST /api/vitals`
- `alerts.json` — consumed on `get_alerts` tool call

Both files use atomic writes (write to `.tmp`, then `os.replace`) for
crash safety. All file access is guarded by `threading.Lock`.

## File Structure

```
vitalcast/
├── vitalcast_mcp.py          # Entry point: MCP server + HTTP thread
├── server/
│   ├── __init__.py            # Package marker
│   ├── config.py              # VitalCastConfig (env + CLI)
│   ├── http_handler.py        # VitalCastHTTPHandler (std lib)
│   ├── models.py              # HealthSample, AlertEvent dataclasses
│   └── storage.py             # VitalsStore + AlertsStore (thread-safe)
├── pyproject.toml             # Package metadata
└── README.md                  # This file
```
