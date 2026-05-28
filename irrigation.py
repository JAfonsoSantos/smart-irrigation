#!/usr/bin/env python3
"""
Smart Irrigation Controller for Esposende, PT.
Designed to run as a GitHub Actions cron job (no PC required).

Reads weather from Open-Meteo, decides irrigation strategy, executes
sequentially via Shelly Cloud API, logs the run, and notifies Slack.

Secrets expected in environment:
  SHELLY_AUTH_KEY     - Shelly cloud auth_key
  SLACK_WEBHOOK_URL   - Incoming webhook URL for DM (optional but recommended)
"""

import os
import sys
import json
import time
import datetime
import urllib.parse
import urllib.request

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
LAT, LON = 41.5463, -8.7882
SHELLY_BASE = "https://shelly-46-eu.shelly.cloud"
AUTH_KEY = os.environ.get("SHELLY_AUTH_KEY", "").strip()
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
LOG_PATH = os.environ.get("IRRIGATION_LOG", "irrigation-log.json")

ZONES = [
    {"name": "oliveira", "device": "c8c9a379ff9d", "channel": 0, "use_timer": True},
    {"name": "norte",    "device": "c8c9a379ff9d", "channel": 1, "use_timer": False},
    {"name": "entrada",  "device": "c8c9a37a09c4", "channel": 0, "use_timer": True},
    {"name": "cozinha",  "device": "c8c9a37a09c4", "channel": 1, "use_timer": False},
]

# -----------------------------------------------------------------------------
# HTTP helpers (no external deps)
# -----------------------------------------------------------------------------
def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "smart-irrigation/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def http_post_form(url, data, timeout=20):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                  headers={"Content-Type": "application/x-www-form-urlencoded",
                                           "User-Agent": "smart-irrigation/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

# -----------------------------------------------------------------------------
# Weather + decision
# -----------------------------------------------------------------------------
def fetch_weather():
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}"
           "&daily=precipitation_sum,precipitation_probability_max,et0_fao_evapotranspiration"
           "&hourly=precipitation&past_days=7&forecast_days=2&timezone=Europe/Lisbon")
    return http_get(url)

def decide(weather):
    daily = weather["daily"]
    today_idx = daily["time"].index(datetime.date.today().isoformat())

    rain_48h = sum(daily["precipitation_sum"][today_idx-2:today_idx])  # yesterday + day before
    rain_forecast_12h = daily["precipitation_sum"][today_idx] * 0.5  # rough estimate of today's first half
    forecast_probability = daily["precipitation_probability_max"][today_idx] or 0
    et0_today = daily["et0_fao_evapotranspiration"][today_idx] or 0

    # Count consecutive dry days looking back
    dry_days = 0
    for i in range(today_idx - 1, -1, -1):
        if (daily["precipitation_sum"][i] or 0) < 1:
            dry_days += 1
        else:
            break

    # Decision rules
    if rain_48h > 5 or (forecast_probability > 70 and rain_forecast_12h > 3):
        decision = "SKIP"; ch0_timer = 0
    elif 2 <= rain_48h <= 5:
        decision = "REDUCED"; ch0_timer = 450
    elif dry_days >= 5 and et0_today > 4:
        decision = "EXTENDED"; ch0_timer = 1350
    else:
        decision = "NORMAL"; ch0_timer = 900

    # Seasonal adjustment (channel 0 only)
    month = datetime.date.today().month
    if decision != "SKIP":
        if 6 <= month <= 9:
            ch0_timer = int(ch0_timer * 1.3)
        elif month in (12, 1, 2):
            ch0_timer = int(ch0_timer * 0.7)

    # Safety cap
    ch0_timer = min(ch0_timer, 1500)

    return {
        "decision": decision,
        "ch0_timer": ch0_timer,
        "ch1_duration": 900,  # device built-in auto-off
        "rain_48h": round(rain_48h, 2),
        "rain_forecast_12h": round(rain_forecast_12h, 2),
        "forecast_probability": int(forecast_probability),
        "dry_days": dry_days,
        "et0": round(et0_today, 2),
    }

# -----------------------------------------------------------------------------
# Shelly control
# -----------------------------------------------------------------------------
def shelly_on(device, channel, timer=None):
    data = {"auth_key": AUTH_KEY, "id": device, "channel": channel, "turn": "on"}
    if timer:
        data["timer"] = timer
    return http_post_form(f"{SHELLY_BASE}/device/relay/control", data)

def shelly_off(device, channel):
    data = {"auth_key": AUTH_KEY, "id": device, "channel": channel, "turn": "off"}
    return http_post_form(f"{SHELLY_BASE}/device/relay/control", data)

def shelly_status(device):
    data = {"auth_key": AUTH_KEY, "id": device}
    return http_post_form(f"{SHELLY_BASE}/device/status", data)

def get_relay_state(device, channel):
    try:
        st = shelly_status(device)
        relays = st.get("data", {}).get("device_status", {}).get("relays", [])
        if relays and len(relays) > channel:
            return relays[channel].get("ison")
    except Exception as e:
        print(f"  status check error: {e}")
    return None

def turn_off_with_retry(device, channel, name, attempts=3):
    for i in range(1, attempts + 1):
        try:
            shelly_off(device, channel)
        except Exception as e:
            print(f"  [{name}] off attempt {i} error: {e}")
        time.sleep(3)
        state = get_relay_state(device, channel)
        print(f"  [{name}] off attempt {i}: ison={state}")
        if state is False:
            return True
        time.sleep(3)
    return False

def run_zone(zone, ch0_timer):
    name = zone["name"]
    device = zone["device"]
    channel = zone["channel"]
    use_timer = zone["use_timer"]
    duration = ch0_timer if use_timer else 900

    print(f"\n--- Zone: {name} (device {device} ch{channel}, {duration}s) ---")
    try:
        if use_timer:
            shelly_on(device, channel, timer=ch0_timer)
        else:
            shelly_on(device, channel)
    except Exception as e:
        print(f"  ON error: {e}")
        return False

    time.sleep(3)
    state = get_relay_state(device, channel)
    print(f"  ON verify: ison={state}")
    if state is not True:
        print(f"  WARN: zone {name} did not turn on")
        return False

    # Wait full duration
    time.sleep(duration)

    # Turn off with retry (channel 1 should already auto-off, but verify)
    ok = turn_off_with_retry(device, channel, name)
    if not ok:
        print(f"  WARN: zone {name} could not be confirmed OFF")
    time.sleep(10)  # pause before next zone
    return ok

# -----------------------------------------------------------------------------
# Logging + Slack
# -----------------------------------------------------------------------------
def append_log(entry):
    entries = []
    if os.path.exists(LOG_PATH):
        try:
            with open(LOG_PATH) as f:
                entries = json.load(f)
        except Exception:
            entries = []
    entries.append(entry)
    entries = entries[-90:]
    with open(LOG_PATH, "w") as f:
        json.dump(entries, f, indent=2)

def slack_notify(text):
    if not SLACK_WEBHOOK:
        print("(no SLACK_WEBHOOK_URL set, skipping notification)")
        return
    body = json.dumps({"text": text}).encode()
    req = urllib.request.Request(SLACK_WEBHOOK, data=body, method="POST",
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
    except Exception as e:
        print(f"Slack notify error: {e}")

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main():
    if not AUTH_KEY:
        print("ERROR: SHELLY_AUTH_KEY not set", file=sys.stderr)
        sys.exit(1)

    try:
        weather = fetch_weather()
        plan = decide(weather)
    except Exception as e:
        print(f"Weather fetch failed ({e}) — defaulting to NORMAL")
        plan = {"decision": "NORMAL", "ch0_timer": 900, "ch1_duration": 900,
                "rain_48h": -1, "rain_forecast_12h": -1, "forecast_probability": -1,
                "dry_days": -1, "et0": -1}
        # apply seasonal
        m = datetime.date.today().month
        if 6 <= m <= 9: plan["ch0_timer"] = int(plan["ch0_timer"] * 1.3)
        elif m in (12, 1, 2): plan["ch0_timer"] = int(plan["ch0_timer"] * 0.7)

    print(f"Decision: {plan['decision']}")
    print(f"  rain_48h={plan['rain_48h']}mm  forecast_12h={plan['rain_forecast_12h']}mm "
          f"prob={plan['forecast_probability']}%  dry_days={plan['dry_days']}  et0={plan['et0']}mm")
    print(f"  ch0_timer={plan['ch0_timer']}s  ch1_duration={plan['ch1_duration']}s")

    zones_watered = []
    total_minutes = 0

    if plan["decision"] != "SKIP":
        for zone in ZONES:
            ok = run_zone(zone, plan["ch0_timer"])
            zones_watered.append(zone["name"])
            total_minutes += (plan["ch0_timer"] if zone["use_timer"] else 900) / 60.0

    # Final safety check
    print("\n=== Final safety check ===")
    for device in {z["device"] for z in ZONES}:
        try:
            for ch in (0, 1):
                state = get_relay_state(device, ch)
                if state is True:
                    print(f"  WARN: {device} ch{ch} still ON — forcing off")
                    turn_off_with_retry(device, ch, f"{device}-ch{ch}")
                else:
                    print(f"  {device} ch{ch}: ison={state}")
        except Exception as e:
            print(f"  status error for {device}: {e}")

    # Log
    now = datetime.datetime.now()
    entry = {
        "date": now.date().isoformat(),
        "time": now.strftime("%H:%M"),
        "decision": plan["decision"],
        "rain_48h_mm": plan["rain_48h"],
        "forecast_12h_mm": plan["rain_forecast_12h"],
        "forecast_prob_pct": plan["forecast_probability"],
        "dry_days": plan["dry_days"],
        "et0_mm": plan["et0"],
        "zones_watered": zones_watered,
        "duration_per_zone_min": f"{plan['ch0_timer']//60}/{plan['ch1_duration']//60}",
        "total_duration_min": round(total_minutes, 1),
    }
    append_log(entry)
    print(f"\nLogged to {LOG_PATH}")

    # Slack
    if plan["decision"] == "SKIP":
        msg = f"⏭️ Rega SKIP — {plan['rain_48h']}mm nas últimas 48h."
    else:
        ch0_min = plan["ch0_timer"] // 60
        msg = (f"🌱 Rega: *{plan['decision']}* — 4 zonas concluídas "
               f"(~{ch0_min}min ch0 / 15min ch1, total ~{round(total_minutes)}min). "
               f"Chuva 48h: {plan['rain_48h']}mm · et0: {plan['et0']}mm · dias secos: {plan['dry_days']}.")
    slack_notify(msg)
    print(f"\nSlack: {msg}")

if __name__ == "__main__":
    main()
