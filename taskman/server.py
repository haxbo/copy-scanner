"""
HaxFish Task Manager — local process manager for all mirofish tasks.

Lists all running Python processes from this project, plus known tasks
from settings.json. Allows stop/start via API.

Usage:
    python taskman/server.py          # starts on port 8081
"""

import json
import math
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

PROJECT_DIR = Path(__file__).parent.parent
SETTINGS_PATH = PROJECT_DIR / "settings.json"
LOGS_DIR = PROJECT_DIR / "logs"
TASKMAN_DIR = Path(__file__).parent
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"

# Known tasks: name -> {script, args, log, enabled_key}
KNOWN_TASKS = {
    "scheduler": {
        "script": "scheduler.py",
        "args": [],
        "log": "monitor.log",
        "enabled_key": None,  # always runs
    },
    "copy_scanner": {
        "script": "copy_scanner.py",
        "args": ["monitor"],
        "log": "copy_scanner.log",
        "enabled_key": "copy_enabled",
    },
    "xarb_scanner": {
        "script": "cross_platform_arb.py",
        "args": ["monitor"],
        "log": "xarb_scanner.log",
        "enabled_key": "xarb_enabled",
    },
    "web_api": {
        "script": "Backend/web_api.py",
        "args": [],
        "log": "webapi.log",
        "enabled_key": None,
    },
    "poly_scanner": {
        "script": "polymarket_scanner.py",
        "args": ["monitor"],
        "log": "scan.log",
        "enabled_key": "poly_scan_enabled",
    },
    "betfair_scanner": {
        "script": "betfair_scanner.py",
        "args": ["monitor"],
        "log": "monitor.log",
        "enabled_key": "betfairEnabled",
    },
    "nba_scanner": {
        "script": "nba_scanner.py",
        "args": ["monitor"],
        "log": "monitor.log",
        "enabled_key": "nba_enabled",
    },
    "cross_arb_scanner": {
        "script": "cross_arb_scanner.py",
        "args": ["monitor"],
        "log": "monitor.log",
        "enabled_key": "cross_arb_strict_enabled",
    },
    "sniper_scanner": {
        "script": "sniper_scanner.py",
        "args": [],
        "log": "monitor.log",
        "enabled_key": None,
    },
    "maker_mm": {
        "script": "maker_mm.py",
        "args": [],
        "log": "monitor.log",
        "enabled_key": None,
    },
    "health_check": {
        "script": "health_check.py",
        "args": [],
        "log": "monitor.log",
        "enabled_key": None,
    },
}


def _find_launchd_label(script_name):
    """Find launchd plist label for a given script, if any."""
    if not LAUNCH_AGENTS_DIR.exists():
        return None
    try:
        import plistlib
        for plist in LAUNCH_AGENTS_DIR.glob("com.haxfish*.plist"):
            try:
                with open(plist, "rb") as f:
                    d = plistlib.load(f)
                args = d.get("ProgramArguments", [])
                cmd = " ".join(args)
                if script_name in cmd:
                    return d.get("Label")
            except Exception:
                continue
        for plist in LAUNCH_AGENTS_DIR.glob("com.haxbo*.plist"):
            try:
                with open(plist, "rb") as f:
                    d = plistlib.load(f)
                args = d.get("ProgramArguments", [])
                cmd = " ".join(args)
                if script_name in cmd:
                    return d.get("Label")
            except Exception:
                continue
    except Exception:
        pass
    return None


def _launchctl_stop(label):
    """Unload a launchd agent by label."""
    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{label}"],
        capture_output=True, timeout=10)


def _launchctl_start(label):
    """Load a launchd agent by label."""
    plist_path = LAUNCH_AGENTS_DIR / f"{label}.plist"
    if plist_path.exists():
        uid = os.getuid()
        subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
            capture_output=True, timeout=10)


def _get_settings():
    try:
        return json.loads(SETTINGS_PATH.read_text())
    except Exception:
        return {}


def _save_settings(settings):
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")


# ── Database query API ─────────────────────────────────────────────────────

ALLOWED_TABLES = {
    "copy_scanner": {
        "db": "copy_scanner.db",
        "tables": ["copy_wallets", "copy_positions", "copy_trades", "copy_skips"],
    },
    "cross_platform_arb": {
        "db": "cross_platform_arb.db",
        "tables": ["xarb_scans", "xarb_alerts"],
    },
}

TABLE_FILTERS = {
    "copy_positions": {
        "open": "closed_at IS NULL",
        "closed": "closed_at IS NOT NULL",
    },
    "copy_trades": {
        "open": "status IN ('pending','open')",
        "closed": "status = 'closed'",
    },
}


def _query_table(db_name, table, page=1, per_page=50,
                 sort=None, order="desc", preset=None):
    """Query a whitelisted SQLite table with pagination."""
    if db_name not in ALLOWED_TABLES:
        return {"error": f"Unknown database: {db_name}"}
    info = ALLOWED_TABLES[db_name]
    if table not in info["tables"]:
        return {"error": f"Unknown table: {table}"}

    db_path = PROJECT_DIR / info["db"]
    if not db_path.exists():
        return {"columns": [], "rows": [], "total": 0,
                "page": 1, "per_page": per_page, "pages": 0}

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row

        # Get valid column names
        cols_info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        valid_cols = [c[1] for c in cols_info]
        if not valid_cols:
            conn.close()
            return {"error": f"Table {table} has no columns"}

        # Build WHERE clause from preset
        where = ""
        if preset and table in TABLE_FILTERS and preset in TABLE_FILTERS[table]:
            where = f"WHERE {TABLE_FILTERS[table][preset]}"

        # Validate sort column
        if sort and sort in valid_cols:
            sort_col = sort
        else:
            sort_col = valid_cols[0]  # default to first column (usually id)
        order_dir = "ASC" if order.lower() == "asc" else "DESC"

        # Count total
        count_sql = f"SELECT COUNT(*) FROM {table} {where}"
        total = conn.execute(count_sql).fetchone()[0]
        pages = max(1, math.ceil(total / per_page))
        page = max(1, min(page, pages))
        offset = (page - 1) * per_page

        # Fetch page
        query = (f"SELECT * FROM {table} {where} "
                 f"ORDER BY {sort_col} {order_dir} "
                 f"LIMIT {per_page} OFFSET {offset}")
        rows = conn.execute(query).fetchall()

        result = {
            "columns": valid_cols,
            "rows": [list(r) for r in rows],
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": pages,
        }
        conn.close()
        return result
    except Exception as e:
        return {"error": str(e)}


# ── Utility tasks (non-Python) ─────────────────────────────────────────────

UTILITY_TASKS = {
    "caffeine": {
        "display": "Caffeine (prevent sleep)",
        "start_cmd": ["caffeinate", "-di"],
        "match": "caffeinate",
    },
    "ssh_proxy": {
        "display": "SSH SOCKS Proxy (EC2)",
        "start_cmd": [
            "ssh", "-i", os.path.expanduser("~/Downloads/mirofish-trading.pem"),
            "-D", "9090", "-N", "-f",
            "ec2-user@34.242.189.188",
        ],
        "match": "ssh.*-D 9090",
    },
}


def _find_utility_process(match_pattern):
    """Find a running process matching a pattern. Returns (pid, cpu, mem) or None."""
    try:
        result = subprocess.run(
            ["ps", "aux"], capture_output=True, text=True, timeout=5)
        for line in result.stdout.strip().split("\n"):
            if "grep" in line:
                continue
            if re.search(match_pattern, line):
                parts = line.split(None, 10)
                if len(parts) >= 4:
                    return {"pid": int(parts[1]), "cpu": parts[2], "mem": parts[3]}
    except Exception:
        pass
    return None


def _start_utility(name):
    """Start a utility task."""
    if name not in UTILITY_TASKS:
        return {"ok": False, "error": f"Unknown utility: {name}"}

    util = UTILITY_TASKS[name]

    # Kill existing first (SSH needs this)
    existing = _find_utility_process(util["match"])
    if existing:
        try:
            os.kill(existing["pid"], signal.SIGTERM)
            time.sleep(0.5)
        except ProcessLookupError:
            pass

    try:
        proc = subprocess.Popen(
            util["start_cmd"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # For ssh -f, it forks and the parent exits quickly
        if name == "ssh_proxy":
            proc.wait(timeout=5)
            # Check if the background process started
            time.sleep(0.5)
            check = _find_utility_process(util["match"])
            if check:
                return {"ok": True, "pid": check["pid"], "msg": f"Started {util['display']}"}
            return {"ok": False, "error": "SSH tunnel failed to start"}

        return {"ok": True, "pid": proc.pid, "msg": f"Started {util['display']}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _stop_utility(name):
    """Stop a utility task."""
    if name not in UTILITY_TASKS:
        return {"ok": False, "error": f"Unknown utility: {name}"}

    util = UTILITY_TASKS[name]
    existing = _find_utility_process(util["match"])
    if not existing:
        return {"ok": True, "msg": f"{util['display']} not running"}

    try:
        os.kill(existing["pid"], signal.SIGTERM)
        time.sleep(0.3)
        try:
            os.kill(existing["pid"], 0)
            os.kill(existing["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
        return {"ok": True, "msg": f"Stopped {util['display']}"}
    except ProcessLookupError:
        return {"ok": True, "msg": f"{util['display']} already stopped"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _find_processes():
    """Find running Python processes from this project."""
    procs = {}
    try:
        result = subprocess.run(
            ["ps", "aux"], capture_output=True, text=True, timeout=5)
        for line in result.stdout.strip().split("\n"):
            if "mirofish" not in line or "grep" in line:
                continue
            if "python" not in line.lower():
                continue
            parts = line.split(None, 10)
            if len(parts) < 11:
                continue
            pid = int(parts[1])
            cpu = parts[2]
            mem = parts[3]
            cmd = parts[10]

            # Match to known tasks
            for name, task in KNOWN_TASKS.items():
                if task["script"] in cmd:
                    procs[name] = {
                        "pid": pid,
                        "cpu": cpu,
                        "mem": mem,
                        "cmd": cmd.strip(),
                    }
                    break
            else:
                # Unknown mirofish python process
                # Extract script name from command
                script = "unknown"
                for part in cmd.split():
                    if part.endswith(".py"):
                        script = os.path.basename(part)
                        break
                if script != "unknown" and "taskman/server.py" not in cmd:
                    key = script.replace(".py", "")
                    if key not in procs:
                        procs[key] = {
                            "pid": pid,
                            "cpu": cpu,
                            "mem": mem,
                            "cmd": cmd.strip(),
                        }
    except Exception:
        pass
    return procs


def _get_log_tail(log_file, lines=50):
    """Get last N lines of a log file."""
    log_path = LOGS_DIR / log_file
    if not log_path.exists():
        return f"No log file: {log_file}"
    try:
        result = subprocess.run(
            ["tail", f"-{lines}", str(log_path)],
            capture_output=True, text=True, timeout=5)
        return result.stdout
    except Exception as e:
        return f"Error reading log: {e}"


def _get_log_last_line(log_file):
    """Get last meaningful line of a log file."""
    log_path = LOGS_DIR / log_file
    if not log_path.exists():
        return ""
    try:
        result = subprocess.run(
            ["tail", "-1", str(log_path)],
            capture_output=True, text=True, timeout=5)
        return result.stdout.strip()
    except Exception:
        return ""


def _build_task_list():
    """Build full task list with status."""
    settings = _get_settings()
    procs = _find_processes()
    tasks = []

    for name, task in KNOWN_TASKS.items():
        proc = procs.pop(name, None)
        enabled = True
        if task["enabled_key"]:
            enabled = settings.get(task["enabled_key"], False)

        status = "running" if proc else "stopped"
        last_log = _get_log_last_line(task["log"]) if task["log"] else ""

        tasks.append({
            "name": name,
            "script": task["script"],
            "log_file": task["log"],
            "enabled_key": task["enabled_key"],
            "enabled": enabled,
            "status": status,
            "pid": proc["pid"] if proc else None,
            "cpu": proc["cpu"] if proc else "0",
            "mem": proc["mem"] if proc else "0",
            "last_log": last_log,
        })

    # Add any other mirofish processes we found
    for name, proc in procs.items():
        tasks.append({
            "name": name,
            "script": name + ".py",
            "log_file": None,
            "enabled_key": None,
            "enabled": True,
            "status": "running",
            "pid": proc["pid"],
            "cpu": proc["cpu"],
            "mem": proc["mem"],
            "last_log": proc["cmd"],
        })

    # ── Utility tasks (non-Python) ──
    for name, util in UTILITY_TASKS.items():
        proc = _find_utility_process(util["match"])
        tasks.append({
            "name": name,
            "script": util["display"],
            "log_file": None,
            "enabled_key": None,
            "enabled": True,
            "status": "running" if proc else "stopped",
            "pid": proc["pid"] if proc else None,
            "cpu": proc["cpu"] if proc else "0",
            "mem": proc["mem"] if proc else "0",
            "last_log": "",
        })

    return tasks


def _start_task(name):
    """Start a known task."""
    if name not in KNOWN_TASKS:
        return {"ok": False, "error": f"Unknown task: {name}"}

    task = KNOWN_TASKS[name]
    script = str(PROJECT_DIR / task["script"])
    log_path = str(LOGS_DIR / task["log"]) if task["log"] else "/dev/null"

    # Enable in settings if there's an enabled_key
    if task["enabled_key"]:
        settings = _get_settings()
        settings[task["enabled_key"]] = True
        _save_settings(settings)

    # Check if already running
    procs = _find_processes()
    if name in procs:
        return {"ok": True, "msg": f"{name} already running (PID {procs[name]['pid']})"}

    # Try launchd first
    label = _find_launchd_label(task["script"])
    if label:
        _launchctl_start(label)
        time.sleep(1)
        procs = _find_processes()
        if name in procs:
            return {"ok": True, "pid": procs[name]["pid"],
                    "msg": f"Started {name} via launchd ({label})"}

    # Fallback: start directly
    with open(log_path, "a") as lf:
        proc = subprocess.Popen(
            [sys.executable, "-u", script] + task["args"],
            stdout=lf, stderr=lf,
            cwd=str(PROJECT_DIR),
            start_new_session=True,
        )

    return {"ok": True, "pid": proc.pid, "msg": f"Started {name} (PID {proc.pid})"}


def _stop_task(name):
    """Stop a task — via launchctl if managed, otherwise kill."""
    procs = _find_processes()

    # Disable in settings
    if name in KNOWN_TASKS and KNOWN_TASKS[name]["enabled_key"]:
        settings = _get_settings()
        settings[KNOWN_TASKS[name]["enabled_key"]] = False
        _save_settings(settings)

    if name not in procs:
        return {"ok": True, "msg": f"{name} not running"}

    pid = procs[name]["pid"]

    # Check if managed by launchd — must unload or it respawns
    script = KNOWN_TASKS[name]["script"] if name in KNOWN_TASKS else name + ".py"
    label = _find_launchd_label(script)
    if label:
        _launchctl_stop(label)
        time.sleep(1)
        # Verify it's gone
        try:
            os.kill(pid, 0)
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return {"ok": True, "msg": f"Stopped {name} (launchd: {label})"}

    # Not launchd — regular kill
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(10):
            time.sleep(0.3)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return {"ok": True, "msg": f"Stopped {name} (PID {pid})"}
        os.kill(pid, signal.SIGKILL)
        return {"ok": True, "msg": f"Force-killed {name} (PID {pid})"}
    except ProcessLookupError:
        return {"ok": True, "msg": f"{name} already stopped"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class TaskHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(TASKMAN_DIR), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/api/tasks":
            self._json_response(_build_task_list())
        elif path.startswith("/api/log/"):
            name = path.split("/api/log/")[1]
            log_file = None
            if name in KNOWN_TASKS:
                log_file = KNOWN_TASKS[name]["log"]
            if log_file:
                self._json_response({"log": _get_log_tail(log_file, 100)})
            else:
                self._json_response({"log": "No log file for this task"})
        elif path.startswith("/api/db/"):
            # /api/db/{db_name}/{table}?page=1&per_page=50&sort=id&order=desc&preset=open
            parts = path.split("/api/db/")[1].split("/")
            if len(parts) == 2:
                db_name, table = parts
                page = int(qs.get("page", [1])[0])
                per_page = int(qs.get("per_page", [50])[0])
                sort = qs.get("sort", [None])[0]
                order = qs.get("order", ["desc"])[0]
                preset = qs.get("preset", [None])[0]
                self._json_response(_query_table(
                    db_name, table, page, per_page, sort, order, preset))
            else:
                self.send_error(400)
        elif path == "/api/settings":
            self._json_response(_get_settings())
        else:
            super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/start/"):
            name = path.split("/api/start/")[1]
            if name in UTILITY_TASKS:
                self._json_response(_start_utility(name))
            else:
                self._json_response(_start_task(name))
        elif path.startswith("/api/stop/"):
            name = path.split("/api/stop/")[1]
            if name in UTILITY_TASKS:
                self._json_response(_stop_utility(name))
            else:
                self._json_response(_stop_task(name))
        elif path == "/api/settings":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                updates = json.loads(body)
                settings = _get_settings()
                settings.update(updates)
                _save_settings(settings)
                self._json_response({"ok": True, "settings": settings})
            except json.JSONDecodeError:
                self._json_response({"ok": False, "error": "Invalid JSON"})
        else:
            self.send_error(404)

    def _json_response(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # quiet


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    port = 8081
    server = ThreadingHTTPServer(("127.0.0.1", port), TaskHandler)
    print(f"  Task Manager running on http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
