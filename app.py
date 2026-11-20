import hmac
import os
import re
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, Response, request, jsonify, render_template
from dotenv import load_dotenv

load_dotenv()

from supabase_client import supabase  # readings are stored in Supabase (Render's disk is not permanent)

# ---------- Config ----------
API_KEY = os.environ.get("GOAT_API_KEY", "change-me-shared-secret")  # must match the key used by the Raspberry Pi script
STALE_AFTER_SECONDS = 15 * 60  # a goat with no update in this long is flagged "offline"

# Alert thresholds - keep in sync with the dashboard
TEMP_LOW_C = 38.0
TEMP_HIGH_C = 40.0
HR_LOW_BPM = 60
HR_HIGH_BPM = 110

# Dashboard login (HTTP Basic). Leave DASHBOARD_PASSWORD empty to keep the dashboard open.
DASH_USER = os.environ.get("DASHBOARD_USER", "admin")
DASH_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

app = Flask(__name__)


def require_dashboard_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if DASH_PASSWORD:
            a = request.authorization
            ok = (a is not None
                  and hmac.compare_digest((a.username or "").encode(), DASH_USER.encode())
                  and hmac.compare_digest((a.password or "").encode(), DASH_PASSWORD.encode()))
            if not ok:
                return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Herd Watch"'})
        return view(*args, **kwargs)
    return wrapped


# ---------- Helpers ----------
def parse_ts(value):
    """Parse ISO timestamps from Supabase (works on any Python 3 version)."""
    s = str(value).replace("Z", "+00:00")
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(\.\d+)?([+-]\d{2}:?\d{2})?$", s)
    if not m:
        raise ValueError("bad timestamp")
    day, clock, frac, tz = m.groups()
    digits = ((frac or ".")[1:] + "000000")[:6]
    tz = tz or "+00:00"
    if len(tz) == 5:                      # +0000 -> +00:00
        tz = tz[:3] + ":" + tz[3:]
    return datetime.fromisoformat(f"{day}T{clock}.{digits}{tz}")


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


def is_stale(received_at_iso, limit=STALE_AFTER_SECONDS):
    try:
        ts = parse_ts(received_at_iso)
    except ValueError:
        return True
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    return age > limit


def row_to_dict(row):
    """row is a dictionary coming from Supabase."""
    return {
        "goat_id": row.get("goat_id"),
        "received_at": row.get("received_at"),
        "gps": {
            "valid": bool(row.get("gps_valid")),
            "lat": row.get("lat"),
            "lon": row.get("lon"),
            "altitude_m": row.get("altitude_m"),
            "satellites": row.get("satellites"),
        },
        "temperature_c": row.get("temperature_c"),
        "temperature_valid": bool(row.get("temperature_valid")),
        "heart_rate_bpm": row.get("heart_rate_bpm"),
        "heart_rate_valid": bool(row.get("heart_rate_valid")),
        "battery_v": row.get("battery_v"),
        "battery_pct": row.get("battery_pct"),
    }


# ---------- API: ingest (the Raspberry Pi posts here) ----------
@app.route("/api/data", methods=["POST"])
def ingest_data():
    if request.headers.get("X-API-Key") != API_KEY:
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True)
    if not payload or "goat_id" not in payload:
        return jsonify({"error": "invalid payload"}), 400

    gps = payload.get("gps", {}) or {}

    row = {
        "goat_id": str(payload.get("goat_id")),
        "gps_valid": bool(gps.get("valid")),
        "lat": gps.get("lat"),
        "lon": gps.get("lon"),
        "altitude_m": gps.get("altitude_m"),
        "satellites": gps.get("satellites"),
        "temperature_c": payload.get("temperature_c"),
        "temperature_valid": bool(payload.get("temperature_valid")),
        "heart_rate_bpm": payload.get("heart_rate_bpm"),
        "heart_rate_valid": bool(payload.get("heart_rate_valid")),
        "battery_v": payload.get("battery_v"),
        "battery_pct": payload.get("battery_pct"),
    }
    try:
        supabase.table("readings").insert(row).execute()
    except Exception as e:
        return jsonify({"error": f"database error: {e}"}), 500
    return jsonify({"status": "ok"}), 201


# ---------- API: dashboard reads ----------
@app.route("/api/goats", methods=["GET"])
@require_dashboard_auth
def list_goats():
    """Latest reading for every goat that has ever reported in."""
    try:
        rows = supabase.table("latest_readings").select("*").order("goat_id").execute().data or []
    except Exception as e:
        return jsonify({"error": f"database error: {e}"}), 502

    goats = []
    for row in rows:
        d = row_to_dict(row)
        d["status"] = classify_status(row)
        d["stale"] = is_stale(row["received_at"])
        goats.append(d)
    return jsonify({"goats": goats})


@app.route("/api/history/<goat_id>", methods=["GET"])
@require_dashboard_auth
def goat_history(goat_id):
    hours = request.args.get("hours", default=24, type=int)
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        rows = (supabase.table("readings").select("*")
                .eq("goat_id", goat_id).gte("received_at", since)
                .order("received_at", desc=True).limit(1000).execute().data or [])
    except Exception as e:
        return jsonify({"error": f"database error: {e}"}), 502
    rows.reverse()  # oldest first
    return jsonify({"goat_id": goat_id, "readings": [row_to_dict(r) for r in rows]})


@app.route("/api/goats/<goat_id>/track", methods=["GET"])
@require_dashboard_auth
def goat_track(goat_id):
    """GPS breadcrumb trail for the map."""
    hours = request.args.get("hours", default=24, type=int)
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        rows = (supabase.table("readings").select("lat,lon,received_at")
                .eq("goat_id", goat_id).eq("gps_valid", True).gte("received_at", since)
                .order("received_at", desc=True).limit(1000).execute().data or [])
    except Exception as e:
        return jsonify({"error": f"database error: {e}"}), 502
    rows.reverse()  # oldest first
    return jsonify({
        "goat_id": goat_id,
        "points": [{"lat": r["lat"], "lon": r["lon"], "t": r["received_at"]} for r in rows],
    })


# ---------- Dashboard page ----------
@app.route("/")
@require_dashboard_auth
def dashboard():
    return render_template("herd_watch_dashboard.html")


if __name__ == "__main__":
    # Local testing only. On Render the app is started by gunicorn (gunicorn app:app).
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)),
            debug=os.environ.get("FLASK_DEBUG") == "1", threaded=True)
