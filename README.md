# ISP Monitor

A self-hosted internet speed monitoring tool that runs continuous Ookla speedtests and per-connection ping checks, includes a built-in iperf3 server, and displays the results in a live web dashboard.

<img width="1914" height="768" alt="image" src="https://github.com/user-attachments/assets/b614a734-471a-45bc-9092-25084efc5361" />


## What it does

ISP Monitor periodically runs the [Ookla Speedtest CLI](https://www.speedtest.net/apps/cli) and `ping` in the background, stores every result in a local SQLite database, and serves a Flask web dashboard where you can view historical data, spot outages, run iperf3 bandwidth tests, and manage everything from the browser — no config files needed.

Key capabilities:

- **Multi-connection monitoring** — define any number of connections (network interfaces), each with its own alias, ping target, and optional speedtest
- **Continuous speedtests** — configurable interval (1 min – 1 h), automatic server fallback, runs 24/7
- **Continuous ping checks** — per-connection latency and packet-loss tracking to any host (e.g. `8.8.8.8` or a game server IP)
- **Live dashboard** — auto-refreshes every 5 minutes and immediately when a new test completes
- **Three charts** — Download speed, Upload speed, and Ping latency; filterable by 1 h / 6 h / 24 h / 7 d / 30 d; filterable by connection
- **Connection status cards** — coloured per-connection ping cards (green / amber / red) showing current RTT, average RTT, and packet loss
- **Outage detection** — failed tests recorded with `success=0` and shown as red dots on charts
- **In-browser settings** — all configuration lives in the database and is editable from the settings panel without restarting anything
- **CSV export** — download raw measurements for any time range and connection from the settings panel
- **Automatic server fallback** — if the preferred server fails, the monitor tries up to N nearest servers before recording a failure row
- **Data retention** — optionally auto-purge records older than N days
- **First-run wizard** — a welcome popup guides new users to settings on the very first visit
- **Timezone** — select your timezone in settings; timestamps are stored in that timezone and tooltips display accordingly
- **Per-connection server pinning** — assign specific Ookla server IDs to each connection; the monitor tests against each pinned server individually via `--interface` and `--server-id`
- **Global server pinning** — select multiple preferred Ookla servers for the global (no-connections) speedtest mode; the monitor cycles through each one every measurement interval
- **Built-in iperf3 server** — start/stop an iperf3 server from the dashboard's dedicated iperf3 tab; test LAN/WAN throughput from any iperf3 client; results (TCP and UDP) are automatically parsed and stored in the database

## Architecture

```
speedtest CLI  ──►  speedtest_monitor.py  ──►  SQLite (speedtest.db)
ping               (per connection)                     │
                                            dashboard.py (Flask) ◄── iperf3 server
                                                        │
                                                  Browser (Chart.js)
```

Two Python processes share one SQLite database (WAL mode for concurrent access):

| Component | File | Role |
|---|---|---|
| Monitor | `speedtest_monitor.py` | Daemon — runs speedtests + pings, writes results |
| Dashboard | `dashboard.py` | Flask app — reads DB, serves UI and JSON API, manages iperf3 server |

## Requirements

- Docker + Docker Compose (recommended)
- **or** Python 3.11+, the [Ookla Speedtest CLI](https://www.speedtest.net/apps/cli), and `iperf3` installed on `PATH`

---

## Quick start — Docker Hub (easiest)

No need to clone the repo. The pre-built image supports both `linux/amd64` and `linux/arm64`.

### Option A — docker compose (recommended)

Create a `docker-compose.yaml` with the following content and run `docker compose up -d`:

```yaml
services:

  isp_monitor:
    image: daimik/isp-monitor:latest
    container_name: isp_monitor
    restart: unless-stopped
    ports:
      - "5000:5000"
      - "5201:5201"   # iperf3 server
    environment:
      PYTHONUNBUFFERED: "1"
    volumes:
      - db_data:/data
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

volumes:
  db_data: {}
```

```bash
docker compose up -d
```

### Option B — plain docker run

```bash
docker run -d \
  --name isp_monitor \
  --restart unless-stopped \
  -p 5000:5000 \
  -p 5201:5201 \
  -v isp_monitor_data:/data \
  daimik/isp-monitor:latest
```

Open **http://localhost:5000** in your browser. The first speedtest runs immediately on startup. iperf3 is available on port **5201** when enabled from the dashboard.

---

## Quick start — build from source

```bash
git clone https://github.com/daimik/isp-monitor.git
cd isp-monitor
docker compose up -d --build
```

Open **http://localhost:5000** in your browser. The first speedtest runs immediately on startup.

### Updating after code changes

```bash
docker compose restart
```

Rebuild only when `requirements.txt` or the `Dockerfile` changes:

```bash
docker compose up -d --build
```

---

## Useful commands

```bash
docker compose logs -f                  # stream logs
docker compose exec isp_monitor bash    # shell inside container
docker compose down                     # stop and remove container
docker compose pull && docker compose up -d  # update to latest image
```

The SQLite database is stored in the `db_data` Docker named volume and persists across restarts and rebuilds.

---

## Manual / local setup

1. Install the [Ookla Speedtest CLI](https://www.speedtest.net/apps/cli) and `iperf3`, and confirm `speedtest --version` and `iperf3 --version` work.

2. Install Python dependencies:

```bash
pip install -r requirements.txt
```

3. Run the monitor daemon in one terminal:

```bash
DB_PATH=./speedtest.db python speedtest_monitor.py
```

4. Run the dashboard in another terminal:

```bash
DB_PATH=./speedtest.db python dashboard.py
```

Open **http://localhost:5000**.

---

## Configuration

Settings can be changed from the dashboard UI (gear icon) without restarting anything. They are persisted in the database and take effect on the next measurement cycle.

<img width="579" height="878" alt="image" src="https://github.com/user-attachments/assets/55b138c6-0a05-47df-bcf1-2ee803354cf4" />


| Setting | Default | Description |
|---|---|---|
| Timezone | Europe/Amsterdam | IANA timezone for storing timestamps and tooltip display |
| Check Interval | 10 min | How often to run a speedtest per connection |
| Speedtest Servers | Auto | One or more preferred Ookla servers (checkbox list); each checked server is tested every cycle. Leave all unchecked for automatic nearest-server selection with fallback |
| Data Retention | Forever | Auto-delete records older than N days (0 = keep all) |
| Max Server Attempts | 5 | How many fallback servers to try before recording a failure (applies when no servers are pinned) |
| Speedtest Timeout | 120 s | Per-test timeout before aborting |

### Connections

In Settings → **Connections** you define which network interfaces to monitor:

| Field | Example | Description |
|---|---|---|
| Alias | `Home Fiber` | Display name shown in the dashboard |
| Interface | `eth0` | Linux network interface for `ping -I` and `speedtest --interface` |
| Ping Host | `8.8.8.8` | IP or hostname to ping every cycle |
| Speedtest | on | Whether to run a full speedtest on this interface |
| Speedtest Servers | — | Optional: pin one or more Ookla server IDs to this connection. Each pinned server is tested individually via `speedtest --server-id <id> --interface <iface>`. Leave all unchecked to auto-select. |
| Enabled | on | Toggle without deleting the connection |

With no connections configured the monitor falls back to a single global speedtest (legacy mode), using the globally pinned servers if any are selected.

### iperf3

The dashboard includes a built-in iperf3 server that you can start and stop from the **iperf3** tab. When enabled, it listens for incoming iperf3 client connections and automatically parses and stores each session result (TCP and UDP) in the database.

| Setting | Default | Description |
|---|---|---|
| iperf3 Enabled | Off | Whether the iperf3 server starts automatically |
| iperf3 Port | 5201 | TCP/UDP port the iperf3 server listens on |

**Client command examples** (run from another machine, replace `<server-ip>` with the host running ISP Monitor):

```bash
# Basic TCP throughput (default 10 s, single stream)
iperf3 -c <server-ip> -p 5201

# TCP with multiple parallel streams (stress-test the link)
iperf3 -c <server-ip> -p 5201 -P 4

# TCP reverse mode — measure download speed (server sends to client)
iperf3 -c <server-ip> -p 5201 -R

# TCP with a longer duration (30 seconds)
iperf3 -c <server-ip> -p 5201 -t 30

# UDP throughput (unlimited bandwidth target)
iperf3 -c <server-ip> -p 5201 -u -b 0

# UDP with a specific bandwidth target (100 Mbps)
iperf3 -c <server-ip> -p 5201 -u -b 100M

# UDP reverse mode — measure download over UDP
iperf3 -c <server-ip> -p 5201 -u -b 0 -R

# Bidirectional test (upload + download simultaneously)
iperf3 -c <server-ip> -p 5201 --bidir
```

| Flag | Description |
|---|---|
| `-c <ip>` | Connect to server at this address |
| `-p <port>` | Server port (default 5201) |
| `-t <sec>` | Test duration in seconds (default 10) |
| `-P <n>` | Number of parallel streams (default 1) |
| `-R` | Reverse mode — server sends, client receives |
| `--bidir` | Bidirectional — test both directions simultaneously |
| `-u` | Use UDP instead of TCP |
| `-b <rate>` | Target bandwidth: `0` = unlimited, `100M` = 100 Mbps, `1G` = 1 Gbps |
| `-i <sec>` | Reporting interval in seconds (default 1) |
| `-J` | JSON output (useful for scripting) |

> **Tip:** Install `iperf3` on the client with `apt install iperf3` (Debian/Ubuntu), `brew install iperf3` (macOS), or download from [iperf.fr](https://iperf.fr/iperf-download.php) (Windows).

Environment variables (Docker / system-level overrides):

| Variable | Default | Description |
|---|---|---|
| `DB_PATH` | `/data/speedtest.db` | Path to the SQLite database |
| `MEASUREMENT_INTERVAL` | `600` | Seconds between tests (seed value; UI overrides this) |
| `MAX_SERVER_ATTEMPTS` | `5` | Max fallback servers to try per cycle |
| `SPEEDTEST_TIMEOUT` | `120` | Per-test timeout in seconds |

---

## Dashboard API

| Endpoint | Method | Description |
|---|---|---|
| `/api/data?range=<r>&target_id=<id>` | GET | Raw speedtest time-series rows (JSON) |
| `/api/stats?range=<r>&target_id=<id>` | GET | Aggregated speedtest statistics (JSON) |
| `/api/ping-data?range=<r>&target_id=<id>` | GET | Raw ping time-series rows (JSON) |
| `/api/ping-stats?range=<r>&target_id=<id>` | GET | Aggregated ping stats per connection (JSON) |
| `/api/export?range=<r>&target_id=<id>` | GET | Download measurements as CSV |
| `/api/targets` | GET | List all connections |
| `/api/targets` | POST | Create a connection `{alias, interface, ping_host, run_speedtest, enabled, speedtest_servers}` |
| `/api/targets/<id>` | PUT | Update a connection |
| `/api/targets/<id>` | DELETE | Delete a connection |
| `/api/servers` | GET | Nearby Ookla servers (cached 1 h) |
| `/api/config` | GET | Read all settings |
| `/api/config` | POST | Write settings |
| `/api/iperf3/status` | GET | iperf3 server status (running, port, pid) |
| `/api/iperf3/toggle` | POST | Start or stop iperf3 server `{enabled, port}` |
| `/api/iperf3/results` | GET | Last 100 iperf3 session results (JSON) |
| `/api/iperf3/results` | DELETE | Clear all iperf3 session records |
| `/health` | GET | Health check |

Valid `range` values: `1h`, `6h`, `24h`, `7d`, `30d`, `all` (export only).
`target_id` is optional; omit for all connections combined.

---

## Backup

The database volume can be backed up with:

```bash
docker run --rm -v isp-monitor_db_data:/data -v $(pwd):/backup alpine \
  tar czf /backup/speedtest_backup.tar.gz /data
```
