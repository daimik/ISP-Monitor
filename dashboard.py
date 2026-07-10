#!/usr/bin/env python3
"""
Speed Test Dashboard - Web interface for viewing speed test results
"""

import atexit
import csv
import datetime
import io
import os
import json
import sqlite3
import subprocess
import threading
import time
from flask import Flask, render_template, jsonify, make_response, request, g, current_app

TIME_RANGE_MAP = {
    '1h':  '-1 hours',
    '6h':  '-6 hours',
    '24h': '-24 hours',
    '7d':  '-7 days',
    '30d': '-30 days',
}

_servers_cache: dict = {'data': None, 'ts': 0}
SERVERS_CACHE_TTL = 3600  # 1 hour

# iperf3 process state (module-level so it survives requests)
_iperf3_proc:   subprocess.Popen | None = None
_iperf3_thread: threading.Thread | None = None
_db_path:       str | None = None


# ---------------------------------------------------------------------------
# Database — per-request connection via Flask g
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    if 'db' not in g:
        conn = sqlite3.connect(current_app.config['DB_PATH'])
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        g.db = conn
    return g.db


def _ensure_config_table() -> None:
    """Seed config table with defaults and run additive DB migrations on first run."""
    db = get_db()
    with db:
        db.execute('''
            CREATE TABLE IF NOT EXISTS config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        ''')
        seeds = [
            ('interval',            '600'),
            ('server_id',           ''),
            ('server_ids',          '[]'),
            ('max_server_attempts', '5'),
            ('speedtest_timeout',   '120'),
            ('retention_days',      '0'),
            ('setup_done',          '0'),
            ('timezone',            'Europe/Amsterdam'),
            ('iperf3_enabled',      '0'),
            ('iperf3_port',         '5201'),
        ]
        for key, value in seeds:
            db.execute(
                "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
                (key, value)
            )
        db.execute('''
            CREATE TABLE IF NOT EXISTS targets (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                alias         TEXT    NOT NULL,
                interface     TEXT    NOT NULL,
                ping_host     TEXT    NOT NULL,
                run_speedtest INTEGER DEFAULT 1,
                enabled       INTEGER DEFAULT 1
            )
        ''')
        db.execute('''
            CREATE TABLE IF NOT EXISTS ping_results (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id   INTEGER NOT NULL,
                timestamp   TEXT    NOT NULL,
                ping_ms     REAL,
                packet_loss REAL,
                success     INTEGER
            )
        ''')
        try:
            db.execute('ALTER TABLE speedtest ADD COLUMN target_id INTEGER')
        except Exception:
            pass  # column already exists
        try:
            db.execute("ALTER TABLE targets ADD COLUMN speedtest_servers TEXT DEFAULT ''")
        except Exception:
            pass  # column already exists
        db.execute('''
            CREATE TABLE IF NOT EXISTS iperf3_results (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT    NOT NULL,
                client_ip   TEXT,
                protocol    TEXT,
                duration_s  REAL,
                sent_mbps   REAL,
                recv_mbps   REAL,
                retransmits INTEGER,
                jitter_ms   REAL,
                raw_json    TEXT
            )
        ''')


# ---------------------------------------------------------------------------
# Queries — speedtest
# ---------------------------------------------------------------------------

def query_speedtest_data(time_range: str = '24h', target_id: str | None = None) -> list | dict:
    modifier = TIME_RANGE_MAP.get(time_range, '-24 hours')
    try:
        if target_id:
            rows = get_db().execute('''
                SELECT s.timestamp, s.download_mbps, s.upload_mbps, s.ping_ms,
                       s.server_name, s.success, t.alias
                FROM speedtest s
                LEFT JOIN targets t ON s.target_id = t.id
                WHERE replace(substr(s.timestamp,1,19),'T',' ') >= datetime('now', ?)
                  AND s.target_id = ?
                ORDER BY s.timestamp
            ''', (modifier, int(target_id))).fetchall()
        else:
            rows = get_db().execute('''
                SELECT s.timestamp, s.download_mbps, s.upload_mbps, s.ping_ms,
                       s.server_name, s.success, t.alias
                FROM speedtest s
                LEFT JOIN targets t ON s.target_id = t.id
                WHERE replace(substr(s.timestamp,1,19),'T',' ') >= datetime('now', ?)
                ORDER BY s.timestamp
            ''', (modifier,)).fetchall()
        return [
            {
                'time':     row['timestamp'],
                'download': row['download_mbps'],
                'upload':   row['upload_mbps'],
                'ping':     row['ping_ms'],
                'server':   row['server_name'],
                'success':  'true' if row['success'] == 1 else 'false',
                'alias':    row['alias'],
            }
            for row in rows
        ]
    except Exception as e:
        return {'error': str(e)}


def get_stats(time_range: str = '24h', target_id: str | None = None) -> dict:
    modifier = TIME_RANGE_MAP.get(time_range, '-24 hours')
    try:
        if target_id:
            row = get_db().execute('''
                SELECT
                    ROUND(AVG(CASE WHEN download_mbps > 0 THEN download_mbps END), 2) AS avg_download,
                    ROUND(AVG(CASE WHEN upload_mbps   > 0 THEN upload_mbps   END), 2) AS avg_upload,
                    ROUND(AVG(CASE WHEN ping_ms       > 0 THEN ping_ms       END), 2) AS avg_ping,
                    ROUND(MAX(download_mbps), 2)                                       AS max_download,
                    ROUND(MAX(upload_mbps),   2)                                       AS max_upload,
                    ROUND(MIN(CASE WHEN ping_ms > 0 THEN ping_ms END), 2)             AS min_ping,
                    COUNT(*)                                                           AS total_tests,
                    SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END)                      AS failed_tests,
                    (SELECT MAX(timestamp) FROM speedtest WHERE target_id = ?)        AS last_test_time
                FROM speedtest
                WHERE replace(substr(timestamp,1,19),'T',' ') >= datetime('now', ?)
                  AND target_id = ?
            ''', (int(target_id), modifier, int(target_id))).fetchone()
        else:
            row = get_db().execute('''
                SELECT
                    ROUND(AVG(CASE WHEN download_mbps > 0 THEN download_mbps END), 2) AS avg_download,
                    ROUND(AVG(CASE WHEN upload_mbps   > 0 THEN upload_mbps   END), 2) AS avg_upload,
                    ROUND(AVG(CASE WHEN ping_ms       > 0 THEN ping_ms       END), 2) AS avg_ping,
                    ROUND(MAX(download_mbps), 2)                                       AS max_download,
                    ROUND(MAX(upload_mbps),   2)                                       AS max_upload,
                    ROUND(MIN(CASE WHEN ping_ms > 0 THEN ping_ms END), 2)             AS min_ping,
                    COUNT(*)                                                           AS total_tests,
                    SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END)                      AS failed_tests,
                    (SELECT MAX(timestamp) FROM speedtest)                             AS last_test_time
                FROM speedtest
                WHERE replace(substr(timestamp,1,19),'T',' ') >= datetime('now', ?)
            ''', (modifier,)).fetchone()
        return dict(row) if row else {}
    except Exception as e:
        return {'error': str(e)}


# ---------------------------------------------------------------------------
# Queries — ping
# ---------------------------------------------------------------------------

def query_ping_data(time_range: str = '24h', target_id: str | None = None) -> list | dict:
    modifier = TIME_RANGE_MAP.get(time_range, '-24 hours')
    try:
        if target_id:
            rows = get_db().execute('''
                SELECT pr.timestamp, pr.ping_ms, pr.packet_loss, pr.success, t.alias
                FROM ping_results pr
                JOIN targets t ON pr.target_id = t.id
                WHERE replace(substr(pr.timestamp,1,19),'T',' ') >= datetime('now', ?)
                  AND pr.target_id = ?
                ORDER BY pr.timestamp
            ''', (modifier, int(target_id))).fetchall()
        else:
            rows = get_db().execute('''
                SELECT pr.timestamp, pr.ping_ms, pr.packet_loss, pr.success, t.alias
                FROM ping_results pr
                JOIN targets t ON pr.target_id = t.id
                WHERE replace(substr(pr.timestamp,1,19),'T',' ') >= datetime('now', ?)
                ORDER BY pr.timestamp
            ''', (modifier,)).fetchall()
        return [
            {
                'time':        row['timestamp'],
                'ping_ms':     row['ping_ms'],
                'packet_loss': row['packet_loss'],
                'success':     'true' if row['success'] == 1 else 'false',
                'alias':       row['alias'],
            }
            for row in rows
        ]
    except Exception as e:
        return {'error': str(e)}


def get_ping_stats(time_range: str = '24h', target_id: str | None = None) -> list | dict:
    """Return aggregated ping stats per target (or for the given target only)."""
    modifier = TIME_RANGE_MAP.get(time_range, '-24 hours')
    try:
        if target_id:
            rows = get_db().execute('''
                SELECT t.id, t.alias, t.interface, t.ping_host,
                       ROUND(AVG(pr.ping_ms), 1)                                        AS avg_ping,
                       ROUND(AVG(pr.packet_loss), 1)                                    AS avg_loss,
                       SUM(CASE WHEN pr.success = 0 THEN 1 ELSE 0 END)                 AS failed,
                       COUNT(*)                                                          AS total,
                       MAX(pr.timestamp)                                                 AS last_check,
                       (SELECT pr2.ping_ms FROM ping_results pr2
                        WHERE pr2.target_id = t.id ORDER BY pr2.timestamp DESC LIMIT 1) AS last_ping,
                       (SELECT pr2.success FROM ping_results pr2
                        WHERE pr2.target_id = t.id ORDER BY pr2.timestamp DESC LIMIT 1) AS last_success
                FROM targets t
                LEFT JOIN ping_results pr
                    ON pr.target_id = t.id
                   AND replace(substr(pr.timestamp,1,19),'T',' ') >= datetime('now', ?)
                WHERE t.id = ? AND t.enabled = 1
                GROUP BY t.id
            ''', (modifier, int(target_id))).fetchall()
        else:
            rows = get_db().execute('''
                SELECT t.id, t.alias, t.interface, t.ping_host,
                       ROUND(AVG(pr.ping_ms), 1)                                        AS avg_ping,
                       ROUND(AVG(pr.packet_loss), 1)                                    AS avg_loss,
                       SUM(CASE WHEN pr.success = 0 THEN 1 ELSE 0 END)                 AS failed,
                       COUNT(*)                                                          AS total,
                       MAX(pr.timestamp)                                                 AS last_check,
                       (SELECT pr2.ping_ms FROM ping_results pr2
                        WHERE pr2.target_id = t.id ORDER BY pr2.timestamp DESC LIMIT 1) AS last_ping,
                       (SELECT pr2.success FROM ping_results pr2
                        WHERE pr2.target_id = t.id ORDER BY pr2.timestamp DESC LIMIT 1) AS last_success
                FROM targets t
                LEFT JOIN ping_results pr
                    ON pr.target_id = t.id
                   AND replace(substr(pr.timestamp,1,19),'T',' ') >= datetime('now', ?)
                WHERE t.enabled = 1
                GROUP BY t.id
            ''', (modifier,)).fetchall()
        return [dict(row) for row in rows]
    except Exception as e:
        return {'error': str(e)}


# ---------------------------------------------------------------------------
# Server list (cached)
# ---------------------------------------------------------------------------

def fetch_servers() -> list:
    if _servers_cache['data'] is not None and time.time() - _servers_cache['ts'] < SERVERS_CACHE_TTL:
        return _servers_cache['data']
    try:
        result = subprocess.run(
            ['speedtest', '--servers', '--format=json'],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            servers = [
                {
                    'id':       str(s.get('id', '')),
                    'name':     s.get('name', ''),
                    'location': s.get('location', ''),
                    'country':  s.get('country', ''),
                }
                for s in json.loads(result.stdout).get('servers', [])
            ]
            _servers_cache['data'] = servers
            _servers_cache['ts'] = time.time()
            return servers
    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
# iperf3 server management
# ---------------------------------------------------------------------------

def _store_iperf3_result(raw: dict) -> None:
    """Parse one iperf3 --json result and write it to the DB (called from background thread)."""
    if not _db_path:
        return
    try:
        start     = raw.get('start', {})
        end       = raw.get('end', {})
        proto     = start.get('test_start', {}).get('protocol', 'TCP')
        connected = start.get('connected', [{}])
        client_ip = connected[0].get('remote_host') if connected else None

        if proto == 'UDP':
            s        = end.get('sum', {})
            duration = s.get('seconds')
            sent     = s.get('bits_per_second', 0) / 1e6
            recv     = sent
            retrans  = 0
            jitter   = s.get('jitter_ms')
        else:  # TCP
            ss       = end.get('sum_sent', {})
            sr       = end.get('sum_received', {})
            duration = sr.get('seconds') or ss.get('seconds')
            sent     = ss.get('bits_per_second', 0) / 1e6
            recv     = sr.get('bits_per_second', 0) / 1e6
            retrans  = ss.get('retransmits', 0)
            jitter   = None

        ts   = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        conn = sqlite3.connect(_db_path)
        conn.execute('PRAGMA journal_mode=WAL')
        with conn:
            conn.execute(
                '''INSERT INTO iperf3_results
                   (timestamp, client_ip, protocol, duration_s, sent_mbps, recv_mbps, retransmits, jitter_ms, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (ts, client_ip, proto,
                 round(duration, 2) if duration is not None else None,
                 round(sent, 2), round(recv, 2), retrans, jitter,
                 json.dumps(raw))
            )
        conn.close()
    except Exception:
        pass


def _read_iperf3_stdout(proc: subprocess.Popen) -> None:
    """Background thread: read iperf3 stdout and parse complete JSON result objects."""
    buf, depth = '', 0
    try:
        for line in proc.stdout:
            open_b  = line.count('{')
            close_b = line.count('}')
            if depth == 0 and open_b == 0:
                continue  # non-JSON startup/status lines
            buf   += line
            depth += open_b - close_b
            if depth <= 0 and buf.strip().startswith('{'):
                try:
                    _store_iperf3_result(json.loads(buf))
                except Exception:
                    pass
                buf, depth = '', 0
    except Exception:
        pass


def _start_iperf3(port: int) -> None:
    global _iperf3_proc, _iperf3_thread
    _stop_iperf3()
    _iperf3_proc = subprocess.Popen(
        ['iperf3', '--server', '--port', str(port), '--json', '--forceflush'],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    _iperf3_thread = threading.Thread(
        target=_read_iperf3_stdout, args=(_iperf3_proc,), daemon=True
    )
    _iperf3_thread.start()


def _stop_iperf3() -> None:
    global _iperf3_proc
    if _iperf3_proc and _iperf3_proc.poll() is None:
        try:
            _iperf3_proc.terminate()
            _iperf3_proc.wait(timeout=5)
        except Exception:
            pass
    _iperf3_proc = None


atexit.register(_stop_iperf3)


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app() -> Flask:
    app = Flask(__name__)
    app.config['DB_PATH'] = '/data/speedtest.db'

    # Close DB connection at end of every request automatically
    @app.teardown_appcontext
    def close_db(exc: BaseException | None) -> None:
        db = g.pop('db', None)
        if db is not None:
            db.close()

    # JSON error responses for API clients
    @app.errorhandler(404)
    def not_found(e):
        return jsonify(error='Not found'), 404

    @app.errorhandler(500)
    def server_error(e):
        return jsonify(error='Internal server error'), 500

    @app.route('/')
    def index():
        return render_template('dashboard.html')

    @app.route('/api/data')
    def api_data():
        return jsonify(query_speedtest_data(
            request.args.get('range', '24h'),
            request.args.get('target_id') or None,
        ))

    @app.route('/api/stats')
    def api_stats():
        return jsonify(get_stats(
            request.args.get('range', '24h'),
            request.args.get('target_id') or None,
        ))

    @app.route('/api/servers')
    def api_servers():
        return jsonify(fetch_servers())

    # ----- Targets CRUD -----

    @app.route('/api/targets', methods=['GET'])
    def api_targets_list():
        try:
            rows = get_db().execute(
                "SELECT id, alias, interface, ping_host, run_speedtest, enabled, speedtest_servers"
                " FROM targets ORDER BY id"
            ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                try:
                    d['speedtest_servers'] = json.loads(d.get('speedtest_servers') or '[]')
                except Exception:
                    d['speedtest_servers'] = []
                result.append(d)
            return jsonify(result)
        except Exception as e:
            return jsonify(error=str(e)), 500

    @app.route('/api/targets', methods=['POST'])
    def api_targets_create():
        data = request.get_json(silent=True) or {}
        alias = (data.get('alias') or '').strip()
        interface = (data.get('interface') or '').strip()
        ping_host = (data.get('ping_host') or '').strip()
        if not alias or not interface or not ping_host:
            return jsonify(error='alias, interface, and ping_host are required'), 400
        run_speedtest = 1 if data.get('run_speedtest', True) else 0
        enabled = 1 if data.get('enabled', True) else 0
        raw_servers = data.get('speedtest_servers', [])
        speedtest_servers = json.dumps([str(s) for s in raw_servers]) if isinstance(raw_servers, list) else '[]'
        try:
            db = get_db()
            with db:
                cur = db.execute(
                    "INSERT INTO targets (alias, interface, ping_host, run_speedtest, enabled, speedtest_servers)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (alias, interface, ping_host, run_speedtest, enabled, speedtest_servers)
                )
            return jsonify(id=cur.lastrowid, status='created'), 201
        except Exception as e:
            return jsonify(error=str(e)), 500

    @app.route('/api/targets/<int:target_id>', methods=['PUT'])
    def api_targets_update(target_id: int):
        data = request.get_json(silent=True) or {}
        allowed = {'alias', 'interface', 'ping_host', 'run_speedtest', 'enabled', 'speedtest_servers'}
        updates = {k: v for k, v in data.items() if k in allowed}
        if not updates:
            return jsonify(error='No valid fields provided'), 400
        try:
            db = get_db()
            with db:
                for key, value in updates.items():
                    if key == 'speedtest_servers':
                        value = json.dumps([str(s) for s in value]) if isinstance(value, list) else '[]'
                    db.execute(
                        f"UPDATE targets SET {key} = ? WHERE id = ?",
                        (value, target_id)
                    )
            return jsonify(status='ok')
        except Exception as e:
            return jsonify(error=str(e)), 500

    @app.route('/api/targets/<int:target_id>', methods=['DELETE'])
    def api_targets_delete(target_id: int):
        try:
            db = get_db()
            with db:
                db.execute("DELETE FROM targets WHERE id = ?", (target_id,))
            return jsonify(status='deleted')
        except Exception as e:
            return jsonify(error=str(e)), 500

    # ----- Ping data -----

    @app.route('/api/ping-data')
    def api_ping_data():
        return jsonify(query_ping_data(
            request.args.get('range', '24h'),
            request.args.get('target_id') or None,
        ))

    @app.route('/api/ping-stats')
    def api_ping_stats():
        return jsonify(get_ping_stats(
            request.args.get('range', '24h'),
            request.args.get('target_id') or None,
        ))

    # ----- Config -----

    @app.route('/api/config', methods=['GET'])
    def api_get_config():
        try:
            rows = get_db().execute("SELECT key, value FROM config").fetchall()
            return jsonify({row['key']: row['value'] for row in rows})
        except Exception as e:
            return jsonify(error=str(e)), 500

    @app.route('/api/config', methods=['POST'])
    def api_set_config():
        data = request.get_json(silent=True) or {}
        allowed = {'interval', 'server_id', 'server_ids', 'max_server_attempts', 'speedtest_timeout', 'retention_days', 'timezone'}
        try:
            db = get_db()
            with db:
                for key, value in data.items():
                    if key in allowed:
                        db.execute(
                            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                            (key, str(value))
                        )
                # Mark setup as complete the first time any settings are saved
                db.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('setup_done', '1')")
            return jsonify(status='ok')
        except Exception as e:
            return jsonify(error=str(e)), 500

    # ----- Export -----

    @app.route('/api/export')
    def api_export():
        time_range = request.args.get('range', '24h')
        target_id = request.args.get('target_id') or None
        try:
            base_cols = '''
                s.timestamp, s.download_mbps, s.upload_mbps, s.ping_ms, s.jitter_ms,
                s.packet_loss, s.server_id, s.server_name, s.server_location,
                s.isp, s.result_url, s.success, t.alias AS connection
            '''
            join = "LEFT JOIN targets t ON s.target_id = t.id"
            if time_range == 'all':
                if target_id:
                    rows = get_db().execute(
                        f"SELECT {base_cols} FROM speedtest s {join} WHERE s.target_id = ? ORDER BY s.timestamp",
                        (int(target_id),)
                    ).fetchall()
                else:
                    rows = get_db().execute(
                        f"SELECT {base_cols} FROM speedtest s {join} ORDER BY s.timestamp"
                    ).fetchall()
            else:
                modifier = TIME_RANGE_MAP.get(time_range, '-24 hours')
                if target_id:
                    rows = get_db().execute(
                        f"SELECT {base_cols} FROM speedtest s {join} "
                        f"WHERE replace(substr(s.timestamp,1,19),'T',' ') >= datetime('now', ?) "
                        f"  AND s.target_id = ? ORDER BY s.timestamp",
                        (modifier, int(target_id))
                    ).fetchall()
                else:
                    rows = get_db().execute(
                        f"SELECT {base_cols} FROM speedtest s {join} "
                        f"WHERE replace(substr(s.timestamp,1,19),'T',' ') >= datetime('now', ?) "
                        f"ORDER BY s.timestamp",
                        (modifier,)
                    ).fetchall()

            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow([
                'timestamp', 'download_mbps', 'upload_mbps', 'ping_ms', 'jitter_ms',
                'packet_loss', 'server_id', 'server_name', 'server_location',
                'isp', 'result_url', 'success', 'connection',
            ])
            writer.writerows(rows)

            resp = make_response(buf.getvalue())
            resp.headers['Content-Disposition'] = f'attachment; filename=speedtest_{time_range}.csv'
            resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
            return resp
        except Exception as e:
            return jsonify(error=str(e)), 500

    @app.route('/health')
    def health():
        return jsonify(status='ok')

    # ----- iperf3 routes -----

    @app.route('/api/iperf3/status')
    def api_iperf3_status():
        running = _iperf3_proc is not None and _iperf3_proc.poll() is None
        try:
            row = get_db().execute("SELECT value FROM config WHERE key='iperf3_port'").fetchone()
            port = int(row['value']) if row else 5201
        except Exception:
            port = 5201
        return jsonify(running=running, port=port, pid=_iperf3_proc.pid if running else None)

    @app.route('/api/iperf3/toggle', methods=['POST'])
    def api_iperf3_toggle():
        data    = request.get_json(silent=True) or {}
        enabled = bool(data.get('enabled', False))
        port    = int(data.get('port', 5201))
        try:
            db = get_db()
            with db:
                db.execute(
                    "INSERT OR REPLACE INTO config (key, value) VALUES ('iperf3_enabled', ?)",
                    ('1' if enabled else '0',)
                )
                db.execute(
                    "INSERT OR REPLACE INTO config (key, value) VALUES ('iperf3_port', ?)",
                    (str(port),)
                )
            if enabled:
                _start_iperf3(port)
            else:
                _stop_iperf3()
            running = _iperf3_proc is not None and _iperf3_proc.poll() is None
            return jsonify(running=running, port=port)
        except Exception as e:
            return jsonify(error=str(e)), 500

    @app.route('/api/iperf3/results')
    def api_iperf3_results():
        try:
            rows = get_db().execute(
                'SELECT id, timestamp, client_ip, protocol, duration_s,'
                ' sent_mbps, recv_mbps, retransmits, jitter_ms'
                ' FROM iperf3_results ORDER BY id DESC LIMIT 100'
            ).fetchall()
            return jsonify([dict(r) for r in rows])
        except Exception as e:
            return jsonify(error=str(e)), 500

    @app.route('/api/iperf3/results', methods=['DELETE'])
    def api_iperf3_results_clear():
        try:
            db = get_db()
            with db:
                db.execute('DELETE FROM iperf3_results')
            return jsonify(status='ok')
        except Exception as e:
            return jsonify(error=str(e)), 500

    with app.app_context():
        _ensure_config_table()
        global _db_path
        _db_path = app.config['DB_PATH']
        # Auto-start iperf3 if it was enabled in a previous session
        try:
            db = get_db()
            cfg = {r['key']: r['value'] for r in db.execute(
                "SELECT key, value FROM config WHERE key IN ('iperf3_enabled','iperf3_port')"
            ).fetchall()}
            if cfg.get('iperf3_enabled') == '1':
                _start_iperf3(int(cfg.get('iperf3_port', '5201')))
        except Exception:
            pass

    return app


app = create_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
