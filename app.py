import hmac
import os
import re
from datetime import datetime, timedelta, timezone, date, time
from zoneinfo import ZoneInfo
from functools import wraps

from flask import Flask, Response, request, jsonify, render_template
from dotenv import load_dotenv

load_dotenv()

from supabase_client import supabase  # readings are stored in Supabase (Render's disk is not permanent)

# ---------- Config ----------
HISTORY_DELETE_TOKEN = os.environ.get("HISTORY_DELETE_TOKEN", "")
GOAT_RENAME_TOKEN = os.environ.get("GOAT_RENAME_TOKEN", "")
API_KEY = os.environ.get("GOAT_API_KEY", "")  # must match the key used by the Raspberry Pi script
REGISTERED_GOATS = {f'G{i:03d}' for i in range(1, 101)}
# Keep detected collars visible indefinitely; mark them offline after 15 minutes.
ACTIVE_WINDOW_SECONDS = 15 * 60
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
    if not API_KEY or not hmac.compare_digest(request.headers.get("X-API-Key", ""), API_KEY):
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True)
    if not payload or "goat_id" not in payload:
        return jsonify({"error": "invalid payload"}), 400

    if str(payload.get("goat_id")) not in REGISTERED_GOATS:
        return jsonify({"error": "unregistered collar ID"}), 403

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
    """Return registered collars that have transmitted at least once, including offline collars."""
    try:
        rows = supabase.table("latest_readings").select("*").order("goat_id").execute().data or []
    except Exception as e:
        return jsonify({"error": f"database error: {e}"}), 502

    # Display names are metadata; only detected collars in latest_readings are listed.
    try:
        metadata = supabase.table("goat_profiles").select("goat_id,display_name").execute().data or []
        names = {r["goat_id"]: r["display_name"] for r in metadata}
    except Exception:
        app.logger.exception("Unable to load goat names")
        return jsonify({"error": "Could not load goat names. Apply goat_profiles.sql in Supabase."}), 502

    goats = []
    for row in rows:
        if row.get("goat_id") not in REGISTERED_GOATS:
            continue
        d = row_to_dict(row)
        d["name"] = names.get(row["goat_id"], row["goat_id"])
        d["stale"] = is_stale(row.get("received_at"), ACTIVE_WINDOW_SECONDS)
        d["status"] = "offline" if d["stale"] else classify_status(row)
        goats.append(d)
    return jsonify({"goats": goats})


@app.route("/api/goats/<goat_id>/name", methods=["PUT"])
@require_dashboard_auth
def rename_goat(goat_id):
    """Rename a collar without changing its permanent ID or historical readings."""
    if goat_id not in REGISTERED_GOATS:
        return jsonify({"error": "Unregistered collar ID"}), 404
    key = request.headers.get("X-Goat-Rename-Key", "")
    if not GOAT_RENAME_TOKEN or not hmac.compare_digest(key, GOAT_RENAME_TOKEN):
        return jsonify({"error": "Rename not authorized. Configure GOAT_RENAME_TOKEN in Render."}), 403
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
        return jsonify({"error": "Invalid request origin"}), 403
    payload = request.get_json(silent=True) or {}
    name = payload.get("name")
    if not isinstance(name, str):
        return jsonify({"error": "Name must be text"}), 400
    name = name.strip()
    if not (1 <= len(name) <= 40) or any(ord(c) < 32 for c in name):
        return jsonify({"error": "Name must contain 1–40 printable characters"}), 400
    try:
        # Prevent profiles for collars never actually detected by this system.
        existing = (supabase.table("latest_readings").select("goat_id")
                    .eq("goat_id", goat_id).limit(1).execute().data or [])
        if not existing:
            return jsonify({"error": "Collar has not transmitted yet"}), 404
        supabase.table("goat_profiles").upsert({"goat_id": goat_id, "display_name": name}, on_conflict="goat_id").execute()
    except Exception:
        app.logger.exception("Goat rename failed")
        return jsonify({"error": "Could not save goat name. Check goat_profiles table."}), 502
    return jsonify({"goat_id": goat_id, "name": name})


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



# ---------- History by Philippine calendar day ----------
PH_TZ = ZoneInfo("Asia/Manila")


def day_bounds(day_text):
    """Return inclusive/exclusive UTC bounds for a Philippine local calendar day."""
    try:
        day = date.fromisoformat(day_text)
        if day.isoformat() != day_text:
            raise ValueError("invalid format")
    except (TypeError, ValueError):
        return None
    start = datetime.combine(day, time.min, tzinfo=PH_TZ).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


@app.route("/api/history-day/<goat_id>", methods=["GET"])
@require_dashboard_auth
def goat_history_day(goat_id):
    day = request.args.get("date", "")
    bounds = day_bounds(day)
    if not bounds:
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    start, end = bounds
    rows = []
    page_size = 1000
    max_rows = 10000
    try:
        for offset in range(0, max_rows + page_size, page_size):
            batch = (supabase.table("readings").select("*")
                     .eq("goat_id", goat_id)
                     .gte("received_at", start).lt("received_at", end)
                     .order("received_at", desc=False).order("id", desc=False)
                     .range(offset, offset + page_size - 1).execute().data or [])
            rows.extend(batch)
            if len(batch) < page_size:
                break
    except Exception:
        app.logger.exception("History query failed")
        return jsonify({"error": "Could not read history from database"}), 502
    truncated = len(rows) > max_rows
    rows = rows[:max_rows]
    return jsonify({"goat_id": goat_id, "date": day,
                    "readings": [row_to_dict(r) for r in rows],
                    "truncated": truncated})


@app.route("/api/history-day/<goat_id>", methods=["DELETE"])
@require_dashboard_auth
def delete_goat_history_day(goat_id):
    # Dedicated secret; never reuse the Pi's ingestion key or expose this in HTML.
    key = request.headers.get("X-History-Delete-Key", "")
    if not HISTORY_DELETE_TOKEN or not hmac.compare_digest(key, HISTORY_DELETE_TOKEN):
        return jsonify({"error": "Deletion not authorized. Configure HISTORY_DELETE_TOKEN in Render."}), 403
    # Prevent cross-site forms/requests from triggering deletion.
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
        return jsonify({"error": "Invalid request origin"}), 403
    payload = request.get_json(silent=True) or {}
    day = payload.get("date")
    bounds = day_bounds(day)
    if not bounds:
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    start, end = bounds
    try:
        # PostgREST filters delete only the selected goat and Philippine day.
        supabase.table("readings").delete().eq("goat_id", goat_id).gte("received_at", start).lt("received_at", end).execute()
    except Exception:
        app.logger.exception("History deletion failed")
        return jsonify({"error": "Could not delete history"}), 502
    return jsonify({"status": "deleted", "goat_id": goat_id, "date": day})


# ---------- Dashboard page ----------
@app.route("/")
@require_dashboard_auth
def dashboard():
    return render_template("herd_watch_dashboard.html")


if __name__ == "__main__":
    # Local testing only. On Render the app is started by gunicorn (gunicorn app:app).
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)),
            debug=os.environ.get("FLASK_DEBUG") == "1", threaded=True)
