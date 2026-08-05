"""
Flood monitor: fetches river level (JPS Melaka, station 6 -- Lencongan Sg. Malim
di Klebang Besar U/S) and sea tide level (TideCheck), writes data.json for the
dashboard, and sends a Telegram alert when the river crosses UP into a higher
risk tier, the current tide is above threshold, or an upcoming high tide is
predicted to be above threshold.

Required environment variables (set as GitHub Actions secrets -- see README.md,
never hardcode these):
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
  TIDECHECK_API_KEY

Before this is fully live, fill in:
  TIDECHECK_STATION_ID  -- see README.md "Find your TideCheck station"
  TIDE_DANGER_LEVEL_M    -- your own threshold; TideCheck has no flood rating

Note on tide data: TideCheck's free tier returns only high/low extremes, not a
continuous water-level curve. "Current tide" and the trend chart below are
derived by interpolating between the two nearest extremes using the standard
half-cosine approximation of a tide curve (the same simplification most tide
clocks use absent full harmonic-constituent data). It's a good approximation,
not a lab-grade reading.
"""

import os
import math
import json
from datetime import datetime, timedelta, timezone

import requests

RIVER_URL = "https://infobanjirjpsmelaka.water.gov.my/WaterLevel/GetWaterLevelGraph/6"
RIVER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (flood-monitor personal project)",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://infobanjirjpsmelaka.water.gov.my/WaterLevel/Station/6",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}

TIDECHECK_STATION_ID = "keling-141a-mys-uhslc_rq"  # see README.md
TIDE_DANGER_LEVEL_M = 2.0  # tune this yourself
MYT_OFFSET = timedelta(hours=8)  # Malaysia time is fixed UTC+8, no DST

DATA_FILE = "data.json"
STATUS_TIERS = ["normal", "alert", "warning", "danger", "unknown"]


def _to_myt(dt_utc):
    return (dt_utc + MYT_OFFSET).strftime("%Y-%m-%dT%H:%M:%S")


def fetch_river():
    r = requests.get(RIVER_URL, headers=RIVER_HEADERS, timeout=15)
    r.raise_for_status()
    payload = r.json()
    series = [p for p in payload["data"] if p["wl"] != -9999]  # drop offline sentinel
    latest = series[-1] if series else None
    return {
        "stationName": payload["stationName"],
        "series": series,
        "alertSetPoint": payload["alertSetPoint"],
        "warningSetPoint": payload["warningSetPoint"],
        "dangerSetPoint": payload["dangerSetPoint"],
        "latest_wl": latest["wl"] if latest else None,
        "latest_time": latest["timestamp"] if latest else None,
    }


def river_status(river):
    wl = river["latest_wl"]
    if wl is None:
        return "unknown"
    if wl >= river["dangerSetPoint"]:
        return "danger"
    if wl >= river["warningSetPoint"]:
        return "warning"
    if wl >= river["alertSetPoint"]:
        return "alert"
    return "normal"


def fetch_tide(api_key):
    if not TIDECHECK_STATION_ID or "PASTE_YOUR" in TIDECHECK_STATION_ID:
        return None

    r = requests.get(
        f"https://tidecheck.com/api/station/{TIDECHECK_STATION_ID}/tides",
        headers={"X-API-Key": api_key},
        params={"days": 3, "datum": "MSL"},
        timeout=15,
    )
    r.raise_for_status()
    raw = r.json().get("extremes", [])
    if not raw:
        return None

    extremes = sorted(
        (
            {
                "time": datetime.fromisoformat(e["time"].replace("Z", "+00:00")),
                "height": e["height"],
                # TideCheck's marketing snippet uses "H"/"L", their JSON example
                # uses "high"/"low" -- handle either
                "type": "high" if str(e["type"]).lower().startswith("h") else "low",
            }
            for e in raw
        ),
        key=lambda e: e["time"],
    )

    now = datetime.now(timezone.utc)

    def interpolate(t):
        for i in range(len(extremes) - 1):
            a, b = extremes[i], extremes[i + 1]
            if a["time"] <= t <= b["time"]:
                span = (b["time"] - a["time"]).total_seconds()
                frac = (t - a["time"]).total_seconds() / span if span else 0
                return a["height"] + (b["height"] - a["height"]) * (1 - math.cos(math.pi * frac)) / 2
        # t falls outside the fetched window (edge case) -- clamp to nearest extreme
        nearest = min(extremes, key=lambda e: abs((e["time"] - t).total_seconds()))
        return nearest["height"]

    current_height = interpolate(now)
    next_high = next((e for e in extremes if e["type"] == "high" and e["time"] > now), None)

    # sample every 30 min, plus the exact extreme timestamps, so the high/low
    # markers land precisely on the curve instead of snapping to a 30-min tick
    sample_times = []
    t = extremes[0]["time"]
    end = extremes[-1]["time"]
    while t <= end:
        sample_times.append(t)
        t += timedelta(minutes=30)
    sample_times.extend(e["time"] for e in extremes)
    sample_times = sorted(set(sample_times))
    extreme_times = {e["time"] for e in extremes}

    curve = [
        {"time": _to_myt(t), "height": round(interpolate(t), 2), "isExtreme": t in extreme_times}
        for t in sample_times
    ]

    return {
        "current_height_m": round(current_height, 2),
        "next_high_height_m": next_high["height"] if next_high else None,
        "next_high_time": _to_myt(next_high["time"]) if next_high else None,
        "extremes": [
            {"type": e["type"], "time": _to_myt(e["time"]), "height": e["height"]}
            for e in extremes
        ],
        "curve": curve,
    }


def send_telegram(bot_token, chat_id, message):
    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data={"chat_id": chat_id, "text": message},
        timeout=15,
    )
    resp.raise_for_status()


def load_previous(key, default=None):
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                return json.load(f).get(key, default)
        except Exception:
            pass
    return default


def main():
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    tidecheck_key = os.environ.get("TIDECHECK_API_KEY", "")

    if os.environ.get("FORCE_TEST_ALERT") == "true":
        send_telegram(bot_token, chat_id, "Test alert from the flood-monitor Action -- bot connection works.")

    river = fetch_river()
    tide = fetch_tide(tidecheck_key)
    status = river_status(river)
    prev_status = load_previous("river_status", "normal")

    if status != "unknown" and STATUS_TIERS.index(status) > STATUS_TIERS.index(prev_status):
        send_telegram(
            bot_token, chat_id,
            f"River at {river['stationName']}: {river['latest_wl']} m -- now at {status.upper()} level.",
        )

    tide_alerted_next_high_time = None
    if tide is not None:
        if tide["current_height_m"] >= TIDE_DANGER_LEVEL_M:
            send_telegram(
                bot_token, chat_id,
                f"Sea tide near Melaka is currently {tide['current_height_m']} m (threshold {TIDE_DANGER_LEVEL_M} m).",
            )
        elif tide["next_high_height_m"] is not None and tide["next_high_height_m"] >= TIDE_DANGER_LEVEL_M:
            already_alerted = load_previous("tide_alerted_next_high_time")
            if tide["next_high_time"] != already_alerted:
                send_telegram(
                    bot_token, chat_id,
                    f"Heads up: next high tide near Melaka is predicted at {tide['next_high_height_m']} m "
                    f"around {tide['next_high_time']} (threshold {TIDE_DANGER_LEVEL_M} m).",
                )
            tide_alerted_next_high_time = tide["next_high_time"]

    with open(DATA_FILE, "w") as f:
        json.dump(
            {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "river": river,
                "river_status": status,
                "tide": tide,
                "tide_alerted_next_high_time": tide_alerted_next_high_time,
            },
            f,
            indent=2,
        )


if __name__ == "__main__":
    main()
