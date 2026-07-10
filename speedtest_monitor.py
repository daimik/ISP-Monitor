#!/usr/bin/env python3
"""
Speed Test Monitor - Measures internet speed and stores results in SQLite
"""

import os
import re
import time
import json
import sqlite3
import subprocess
import logging
from contextlib import contextmanager
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


DB_PATH = '/data/speedtest.db'

# Seed defaults — written to config table on first run; editable from the UI
_DEFAULT_INTERVAL     = 600   # seconds
_DEFAULT_MAX_ATTEMPTS = 5
_DEFAULT_TIMEOUT      = 120   # seconds
_DEFAULT_TIMEZONE     = 'Europe/Amsterdam'


def _apply_timezone(tz: str) -> None:
    """Apply an IANA timezone to the current process (Linux/macOS only)."""
    try:
        os.environ['TZ'] = tz
        time.tzset()
        logger.info(f"Timezone set to: {tz}")
    except AttributeError:
        pass  # time.tzset() not available on Windows (runs fine in Docker/Linux)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

@contextmanager
def _db_connection():
    """Open a WAL-mode SQLite connection and ensure it is closed on exit."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Create tables and seed config defaults."""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    with _db_connection() as conn:
        with conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS speedtest (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       TEXT    NOT NULL,
                    download_mbps   REAL,
                    upload_mbps     REAL,
                    ping_ms         REAL,
                    jitter_ms       REAL,
                    packet_loss     REAL,
                    server_id       INTEGER,
                    server_name     TEXT,
                    server_location TEXT,
                    isp             TEXT,
                    result_url      TEXT,
                    success         INTEGER
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS config (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            ''')
            # Seed all configurable settings with defaults
            seeds = [
                ('interval',            str(_DEFAULT_INTERVAL)),
                ('server_id',           ''),
                ('server_ids',          '[]'),
                ('max_server_attempts', str(_DEFAULT_MAX_ATTEMPTS)),
                ('speedtest_timeout',   str(_DEFAULT_TIMEOUT)),
                ('retention_days',      '0'),
                ('setup_done',          '0'),
                ('timezone',            _DEFAULT_TIMEZONE),
            ]
            for key, value in seeds:
                conn.execute(
                    "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
                    (key, value)
                )
            conn.execute('''
                CREATE TABLE IF NOT EXISTS targets (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    alias         TEXT    NOT NULL,
                    interface     TEXT    NOT NULL,
                    ping_host     TEXT    NOT NULL,
                    run_speedtest INTEGER DEFAULT 1,
                    enabled       INTEGER DEFAULT 1
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS ping_results (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_id   INTEGER NOT NULL,
                    timestamp   TEXT    NOT NULL,
                    ping_ms     REAL,
                    packet_loss REAL,
                    success     INTEGER
                )
            ''')
            # Additive migration: add target_id to speedtest if it doesn't exist yet
            try:
                conn.execute('ALTER TABLE speedtest ADD COLUMN target_id INTEGER')
            except Exception:
                pass  # column already exists
            # Additive migration: per-target speedtest server list
            try:
                conn.execute("ALTER TABLE targets ADD COLUMN speedtest_servers TEXT DEFAULT ''")
            except Exception:
                pass  # column already exists

    logger.info(f"Database ready: {DB_PATH}")


def get_config() -> dict:
    """Read config from DB each cycle so UI changes apply without a restart."""
    try:
        with _db_connection() as conn:
            rows = conn.execute("SELECT key, value FROM config").fetchall()
            return {row[0]: row[1] for row in rows}
    except Exception as e:
        logger.warning(f"Could not read config, using defaults: {e}")
        return {
            'interval':            str(_DEFAULT_INTERVAL),
            'server_id':           '',
            'max_server_attempts': str(_DEFAULT_MAX_ATTEMPTS),
            'speedtest_timeout':   str(_DEFAULT_TIMEOUT),
            'retention_days':      '0',
        }


def get_targets() -> list[dict]:
    """Return all enabled targets from the DB."""
    try:
        with _db_connection() as conn:
            rows = conn.execute(
                "SELECT id, alias, interface, ping_host, run_speedtest, speedtest_servers"
                " FROM targets WHERE enabled = 1"
            ).fetchall()
            result = []
            for row in rows:
                try:
                    servers = json.loads(row[5] or '[]')
                except Exception:
                    servers = []
                result.append({
                    'id':                row[0],
                    'alias':             row[1],
                    'interface':         row[2],
                    'ping_host':         row[3],
                    'run_speedtest':     row[4],
                    'speedtest_servers': servers,
                })
            return result
    except Exception as e:
        logger.warning(f"Could not read targets: {e}")
        return []


def write_to_db(data: dict, target_id: int | None = None) -> bool:
    """Write a speedtest result row."""
    try:
        with _db_connection() as conn:
            with conn:
                conn.execute('''
                    INSERT INTO speedtest (
                        timestamp, download_mbps, upload_mbps, ping_ms, jitter_ms,
                        packet_loss, server_id, server_name, server_location,
                        isp, result_url, success, target_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    float(data['download']), float(data['upload']),
                    float(data['ping']),     float(data['jitter']),
                    float(data['packet_loss']), int(data['server_id']),
                    data['server_name'], data['server_location'],
                    data['isp'], data['result_url'],
                    1 if data['success'] else 0,
                    target_id,
                ))
        logger.info("Results written to database")
        return True
    except Exception as e:
        logger.error(f"Failed to write to database: {e}")
        return False


def write_ping_to_db(target_id: int, data: dict) -> bool:
    """Write a ping result row."""
    try:
        with _db_connection() as conn:
            with conn:
                conn.execute('''
                    INSERT INTO ping_results (target_id, timestamp, ping_ms, packet_loss, success)
                    VALUES (?, ?, ?, ?, ?)
                ''', (
                    target_id,
                    datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    data.get('ping_ms'),
                    data.get('packet_loss', 100.0),
                    data.get('success', 0),
                ))
        return True
    except Exception as e:
        logger.error(f"Failed to write ping result: {e}")
        return False


# ---------------------------------------------------------------------------
# Ping
# ---------------------------------------------------------------------------

def run_ping(interface: str, host: str) -> dict:
    """Ping a host via a specific network interface. Returns ping_ms, packet_loss, success."""
    try:
        result = subprocess.run(
            ['ping', '-I', interface, '-c', '4', '-W', '2', '-q', host],
            capture_output=True, text=True, timeout=15
        )
        output = result.stdout + result.stderr
        loss_match = re.search(r'(\d+)% packet loss', output)
        rtt_match  = re.search(r'rtt .* = [\d.]+/([\d.]+)/', output)
        packet_loss = float(loss_match.group(1)) if loss_match else 100.0
        ping_ms     = float(rtt_match.group(1))  if rtt_match  else None
        success = 1 if packet_loss < 100 else 0
        logger.info(
            f"Ping {host} via {interface}: "
            f"{'%.1f ms' % ping_ms if ping_ms is not None else 'N/A'}, "
            f"{packet_loss:.0f}% loss"
        )
        return {'ping_ms': ping_ms, 'packet_loss': packet_loss, 'success': success}
    except subprocess.TimeoutExpired:
        logger.warning(f"Ping {host} via {interface} timed out")
        return {'ping_ms': None, 'packet_loss': 100.0, 'success': 0}
    except Exception as e:
        logger.warning(f"Ping {host} via {interface} failed: {e}")
        return {'ping_ms': None, 'packet_loss': 100.0, 'success': 0}


# ---------------------------------------------------------------------------
# Speedtest
# ---------------------------------------------------------------------------

def get_closest_servers(count: int = 10) -> list[int]:
    """Return IDs of the closest speedtest servers."""
    try:
        result = subprocess.run(
            ['speedtest', '--servers', '--format=json'],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            servers = json.loads(result.stdout).get('servers', [])
            logger.info(f"Found {len(servers)} available servers")
            return [s['id'] for s in servers[:count]]
    except subprocess.TimeoutExpired:
        logger.error("Timeout while fetching server list")
    except Exception as e:
        logger.error(f"Error fetching servers: {e}")
    return []


def run_speedtest(
    server_id: int | None = None,
    interface: str | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> dict | None:
    """Run a single speedtest, optionally against a specific server and/or interface."""
    cmd = ['speedtest', '--format=json', '--accept-license', '--accept-gdpr']
    if server_id:
        cmd.extend(['--server-id', str(server_id)])
    if interface:
        cmd.extend(['--interface', interface])

    try:
        logger.info(
            f"Running speedtest"
            f"{f' on server {server_id}' if server_id else ''}"
            f"{f' via {interface}' if interface else ''}..."
        )
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return {
                'download':        data.get('download', {}).get('bandwidth', 0) * 8 / 1_000_000,
                'upload':          data.get('upload',   {}).get('bandwidth', 0) * 8 / 1_000_000,
                'ping':            data.get('ping',     {}).get('latency',   0),
                'jitter':          data.get('ping',     {}).get('jitter',    0),
                'packet_loss':     data.get('packetLoss', 0),
                'server_id':       data.get('server',  {}).get('id',       0),
                'server_name':     data.get('server',  {}).get('name',     'Unknown'),
                'server_location': data.get('server',  {}).get('location', 'Unknown'),
                'isp':             data.get('isp',  'Unknown'),
                'result_url':      data.get('result', {}).get('url', ''),
                'success':         True,
            }
        logger.error(f"Speedtest failed: {result.stderr}")
    except subprocess.TimeoutExpired:
        logger.error(f"Speedtest timed out after {timeout}s")
    except Exception as e:
        logger.error(f"Error running speedtest: {e}")
    return None


def run_speedtest_with_fallback(
    preferred_server_id: int | None = None,
    interface: str | None = None,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    timeout: int = _DEFAULT_TIMEOUT,
) -> dict:
    """
    Run speedtest with fallback:
    1. Preferred server (if set by user).
    2. Auto-select.
    3. Up to max_attempts nearest servers.
    4. Failure row if everything fails.
    """
    if preferred_server_id:
        result = run_speedtest(preferred_server_id, interface=interface, timeout=timeout)
        if result:
            logger.info(
                f"OK on preferred server {preferred_server_id}: "
                f"{result['download']:.2f}↓ {result['upload']:.2f}↑ Mbps, "
                f"{result['ping']:.2f} ms"
            )
            return result
        logger.warning(f"Preferred server {preferred_server_id} failed, falling back")

    result = run_speedtest(interface=interface, timeout=timeout)
    if result:
        logger.info(
            f"OK (auto): {result['download']:.2f}↓ {result['upload']:.2f}↑ Mbps, "
            f"{result['ping']:.2f} ms"
        )
        return result

    for i, sid in enumerate(get_closest_servers(max_attempts), 1):
        logger.info(f"Attempt {i}/{max_attempts}: server {sid}")
        result = run_speedtest(sid, interface=interface, timeout=timeout)
        if result:
            logger.info(f"OK on server {sid}: {result['download']:.2f}↓ {result['upload']:.2f}↑ Mbps")
            return result

    logger.error("All speedtest attempts failed")
    return {
        'download': 0, 'upload': 0, 'ping': 0, 'jitter': 0,
        'packet_loss': 100, 'server_id': 0, 'server_name': 'Failed',
        'server_location': 'N/A', 'isp': 'Unknown', 'result_url': '',
        'success': False,
    }


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

def purge_old_records(retention_days: int) -> None:
    """Delete rows older than retention_days. 0 = keep forever."""
    if retention_days <= 0:
        return
    try:
        with _db_connection() as conn:
            with conn:
                cur = conn.execute(
                    "DELETE FROM speedtest WHERE replace(substr(timestamp,1,19),'T',' ') < datetime('now', ?)",
                    (f'-{retention_days} days',)
                )
                conn.execute(
                    "DELETE FROM ping_results WHERE replace(substr(timestamp,1,19),'T',' ') < datetime('now', ?)",
                    (f'-{retention_days} days',)
                )
        if cur.rowcount:
            logger.info(f"Purged {cur.rowcount} record(s) older than {retention_days} day(s)")
    except Exception as e:
        logger.error(f"Failed to purge old records: {e}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("=" * 60)
    logger.info("Speed Test Monitor Starting")
    logger.info(f"Database: {DB_PATH}")
    logger.info("Settings are read from the database each cycle (configurable via web UI)")
    logger.info("=" * 60)

    init_db()

    # Apply timezone from config before the first measurement
    _apply_timezone(get_config().get('timezone', _DEFAULT_TIMEZONE))

    while True:
        try:
            start_time = time.time()

            config         = get_config()
            interval       = int(config.get('interval',            _DEFAULT_INTERVAL))
            preferred      = config.get('server_id', '').strip()
            preferred_server_id = int(preferred) if preferred else None
            try:
                global_server_ids = json.loads(config.get('server_ids', '[]') or '[]')
            except Exception:
                global_server_ids = []
            retention_days = int(config.get('retention_days',      '0'))
            max_attempts   = int(config.get('max_server_attempts',  str(_DEFAULT_MAX_ATTEMPTS)))
            timeout        = int(config.get('speedtest_timeout',    str(_DEFAULT_TIMEOUT)))

            # Re-apply timezone each cycle so UI changes take effect without a restart
            _apply_timezone(config.get('timezone', _DEFAULT_TIMEZONE))

            targets = get_targets()

            if targets:
                for target in targets:
                    alias     = target['alias']
                    interface = target['interface']
                    ping_host = target['ping_host']
                    target_id = target['id']

                    logger.info(f"--- Target: {alias} ({interface}) ---")

                    # Always run ping
                    ping_result = run_ping(interface, ping_host)
                    write_ping_to_db(target_id, ping_result)

                    # Optionally run speedtest
                    if target['run_speedtest']:
                        selected_servers = target.get('speedtest_servers', [])
                        if selected_servers:
                            # Test against each specifically assigned server
                            for sid in selected_servers:
                                logger.info(f"Testing server {sid} via {interface}...")
                                st_result = run_speedtest(int(sid), interface=interface, timeout=timeout)
                                if st_result:
                                    logger.info(
                                        f"Server {sid}: {st_result['download']:.2f}↓ "
                                        f"{st_result['upload']:.2f}↑ Mbps"
                                    )
                                else:
                                    logger.warning(f"Server {sid} failed via {interface}")
                                    st_result = {
                                        'download': 0, 'upload': 0, 'ping': 0, 'jitter': 0,
                                        'packet_loss': 100, 'server_id': int(sid),
                                        'server_name': 'Failed', 'server_location': 'N/A',
                                        'isp': 'Unknown', 'result_url': '', 'success': False,
                                    }
                                write_to_db(st_result, target_id=target_id)
                        else:
                            # No servers pinned — use global preferred + auto-fallback
                            st_result = run_speedtest_with_fallback(
                                preferred_server_id=preferred_server_id,
                                interface=interface,
                                max_attempts=max_attempts,
                                timeout=timeout,
                            )
                            write_to_db(st_result, target_id=target_id)
            else:
                # Legacy mode: no targets configured
                if global_server_ids:
                    # Test against each pinned global server
                    for sid in global_server_ids:
                        logger.info(f"Global server {sid}: running speedtest...")
                        st_result = run_speedtest(int(sid), timeout=timeout)
                        if st_result:
                            logger.info(
                                f"Server {sid}: {st_result['download']:.2f}↓ "
                                f"{st_result['upload']:.2f}↑ Mbps"
                            )
                        else:
                            logger.warning(f"Server {sid} failed")
                            st_result = {
                                'download': 0, 'upload': 0, 'ping': 0, 'jitter': 0,
                                'packet_loss': 100, 'server_id': int(sid),
                                'server_name': 'Failed', 'server_location': 'N/A',
                                'isp': 'Unknown', 'result_url': '', 'success': False,
                            }
                        write_to_db(st_result)
                else:
                    # No servers pinned — auto-select with fallback
                    results = run_speedtest_with_fallback(
                        preferred_server_id=preferred_server_id,
                        max_attempts=max_attempts,
                        timeout=timeout,
                    )
                    write_to_db(results)

            purge_old_records(retention_days)

            elapsed    = time.time() - start_time
            sleep_time = max(0, interval - elapsed)
            logger.info(f"Next measurement in {sleep_time:.0f}s (interval: {interval}s)")
            time.sleep(sleep_time)

        except KeyboardInterrupt:
            logger.info("Shutting down...")
            break
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            time.sleep(60)


if __name__ == '__main__':
    main()
