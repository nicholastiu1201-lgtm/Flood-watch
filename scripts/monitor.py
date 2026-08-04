"""
Flood monitor: fetches river level (JPS Melaka, station 6 -- Lencongan Sg. Malim
di Klebang Besar U/S) and sea tide level (TideCheck), writes data.json for the
dashboard, and sends a Telegram alert when the river crosses UP into a higher
risk tier, or the tide exceeds a configured threshold.

Required environment variables (set as GitHub Actions secrets -- see README.md,
never hardcode these):
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
  TIDECHECK_API_KEY

Before this is fully live, fill in:
  TIDECHECK_STATION_ID  -- see README.md "Find your TideCheck station"
  TIDE_DANGER_LEVEL_M    -- your own threshold; TideCheck has no flood rating
"""

import os
import json
from datetime import datetime, timezone

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

DATA_FILE = "data.json"
STATUS_TIERS = ["normal", "alert", "warning", "danger", "unknown"]


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
        params={"days": 1, "datum": "MSL"},
        timeout=15,
    )
    r.raise_for_status()
    highs = [e["height"] for e in r.json()["extremes"] if e["type"] == "high"]
    return max(highs) if highs else None


def send_telegram(bot_token, chat_id, message):
    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data={"chat_id": chat_id, "text": message},
        timeout=15,
    )
    resp.raise_for_status()


def load_previous_status():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                return json.load(f).get("river_status", "normal")
        except Exception:
            pass
    return "normal"


def main():
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    tidecheck_key = os.environ.get("TIDECHECK_API_KEY", "")

    if os.environ.get("FORCE_TEST_ALERT") == "true":
        send_telegram(bot_token, chat_id, "Test alert from the flood-monitor Action -- bot connection works.")

    river = fetch_river()
    tide = fetch_tide(tidecheck_key)
    status = river_status(river)
    prev_status = load_previous_status()

    if status != "unknown" and STATUS_TIERS.index(status) > STATUS_TIERS.index(prev_status):
        send_telegram(
            bot_token, chat_id,
            f"River at {river['stationName']}: {river['latest_wl']} m -- now at {status.upper()} level.",
        )
    if tide is not None and tide >= TIDE_DANGER_LEVEL_M:
        send_telegram(
            bot_token, chat_id,
            f"Sea tide near Melaka: {tide} m (threshold {TIDE_DANGER_LEVEL_M} m)",
        )

    with open(DATA_FILE, "w") as f:
        json.dump(
            {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "river": river,
                "river_status": status,
                "tide_level_m": tide,
            },
            f,
            indent=2,
        )


if __name__ == "__main__":
    main()
