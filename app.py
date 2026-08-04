"""
Goat Tracker dashboard server
------------------------------
- Receives POSTed sensor readings from ESP32 collars at /api/data
- Stores them in a local SQLite database
- Serves a JSON API for the dashboard (latest status + history)
- Serves the dashboard itself at /

Run:
    pip install -r requirements.txt
    python app.py

The server listens on 0.0.0.0:5000 by default, so ESP32 collars on the
same network can reach it at http://<this-machine's-LAN-IP>:5000
"""

import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone

from flask import Flask, request, jsonify, render_template, g

# ---------- Config ----------
API_KEY = os.environ.get("GOAT_API_KEY", "change-me-shared-secret")  # must match firmware config.h
DB_PATH = os.path.join(os.path.dirname(__file__), "goat_tracker.db")
STALE_AFTER_SECONDS = 15 * 60  # a goat with no update in this long is flagged "offline"

# Alert thresholds mirror firmware/config.h — keep in sync
TEMP_LOW_C = 38.0
TEMP_HIGH_C = 40.0
HR_LOW_BPM = 60
HR_HIGH_BPM = 110

app = Flask(__name__)


# ---------- Database ----------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                goat_id TEXT NOT NULL,
                received_at TEXT NOT NULL,
                gps_valid INTEGER,
                lat REAL,
                lon REAL,
                altitude_m REAL,
                satellites INTEGER,
                temperature_c REAL,
                temperature_valid INTEGER,
                heart_rate_bpm INTEGER,
                heart_rate_valid INTEGER,
                battery_v REAL
            )
        """)
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_readings_goat_time
            ON readings (goat_id, received_at)
        """)
        db.commit()


# ---------- Helpers ----------
def now_iso():
    return datetime.now(timezone.utc).isoformat()


def classify_status(reading):
    """Return 'alert' | 'warning' | 'ok' based on latest vital signs."""
    if reading is None:
        return "unknown"

    alerts = []
    if reading["temperature_valid"]:
        t = reading["temperature_c"]
        if t < TEMP_LOW_C - 1 or t > TEMP_HIGH_C + 1:
            alerts.append("temperature")
    if reading["heart_rate_valid"]:
        hr = reading["heart_rate_bpm"]
        if hr < HR_LOW_BPM - 15 or hr > HR_HIGH_BPM + 20:
            alerts.append("heart_rate")

    warnings = []
    if reading["temperature_valid"]:
        t = reading["temperature_c"]
        if TEMP_LOW_C - 1 <= t < TEMP_LOW_C or TEMP_HIGH_C < t <= TEMP_HIGH_C + 1:
            warnings.append("temperature")
    if reading["heart_rate_valid"]:
        hr = reading["heart_rate_bpm"]
        if HR_LOW_BPM - 15 <= hr < HR_LOW_BPM or HR_HIGH_BPM < hr <= HR_HIGH_BPM + 20:
            warnings.append("heart_rate")

    if alerts:
        return "alert"
    if warnings:
        return "warning"
    return "ok"


def is_stale(received_at_iso):
    try:
        ts = datetime.fromisoformat(received_at_iso)
    except ValueError:
        return True
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    return age > STALE_AFTER_SECONDS


def row_to_dict(row):
    return {
        "goat_id": row["goat_id"],
        "received_at": row["received_at"],
        "gps": {
            "valid": bool(row["gps_valid"]),
            "lat": row["lat"],
            "lon": row["lon"],
            "altitude_m": row["altitude_m"],
            "satellites": row["satellites"],
        },
        "temperature_c": row["temperature_c"],
        "temperature_valid": bool(row["temperature_valid"]),
        "heart_rate_bpm": row["heart_rate_bpm"],
        "heart_rate_valid": bool(row["heart_rate_valid"]),
        "battery_v": row["battery_v"],
    }


# ---------- API: ingest ----------
@app.route("/api/data", methods=["POST"])
def ingest_data():
    if request.headers.get("X-API-Key") != API_KEY:
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True)
    if not payload or "goat_id" not in payload:
        return jsonify({"error": "invalid payload"}), 400

    gps = payload.get("gps", {}) or {}

    db = get_db()
    db.execute(
        """
        INSERT INTO readings (
            goat_id, received_at, gps_valid, lat, lon, altitude_m, satellites,
            temperature_c, temperature_valid, heart_rate_bpm, heart_rate_valid, battery_v
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(payload.get("goat_id")),
            now_iso(),
            1 if gps.get("valid") else 0,
            gps.get("lat"),
            gps.get("lon"),
            gps.get("altitude_m"),
            gps.get("satellites"),
            payload.get("temperature_c"),
            1 if payload.get("temperature_valid") else 0,
            payload.get("heart_rate_bpm"),
            1 if payload.get("heart_rate_valid") else 0,
            payload.get("battery_v"),
        ),
    )
    db.commit()
    return jsonify({"status": "ok"}), 201


# ---------- API: dashboard reads ----------
@app.route("/api/goats", methods=["GET"])
def list_goats():
    """Latest reading for every goat that has ever reported in."""
    db = get_db()
    rows = db.execute(
        """
        SELECT r.* FROM readings r
        INNER JOIN (
            SELECT goat_id, MAX(id) AS max_id FROM readings GROUP BY goat_id
        ) latest ON r.id = latest.max_id
        ORDER BY r.goat_id
        """
    ).fetchall()

    goats = []
    for row in rows:
        d = row_to_dict(row)
        d["status"] = classify_status(row)
        d["stale"] = is_stale(row["received_at"])
        goats.append(d)
    return jsonify({"goats": goats})


@app.route("/api/history/<goat_id>", methods=["GET"])
def goat_history(goat_id):
    hours = request.args.get("hours", default=24, type=int)
    db = get_db()
    rows = db.execute(
        """
        SELECT * FROM readings
        WHERE goat_id = ?
          AND received_at >= datetime('now', ?)
        ORDER BY received_at ASC
        """,
        (goat_id, f"-{hours} hours"),
    ).fetchall()
    return jsonify({"goat_id": goat_id, "readings": [row_to_dict(r) for r in rows]})


@app.route("/api/goats/<goat_id>/track", methods=["GET"])
def goat_track(goat_id):
    """GPS breadcrumb trail for the map."""
    hours = request.args.get("hours", default=24, type=int)
    db = get_db()
    rows = db.execute(
        """
        SELECT lat, lon, received_at FROM readings
        WHERE goat_id = ? AND gps_valid = 1
          AND received_at >= datetime('now', ?)
        ORDER BY received_at ASC
        """,
        (goat_id, f"-{hours} hours"),
    ).fetchall()
    return jsonify({
        "goat_id": goat_id,
        "points": [{"lat": r["lat"], "lon": r["lon"], "t": r["received_at"]} for r in rows],
    })


# ---------- Dashboard page ----------
@app.route("/")
def dashboard():
    return render_template("dashboard.html")


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
