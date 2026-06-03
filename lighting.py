#!/usr/bin/env python3
"""
Smart Exterior Lighting Controller for Esposende, PT.
Reads config from irrigation-dashboard repo (config.json).

Modes (via env var LIGHTING_MODE):
  - "evening": calculates today's sunset, sleeps until (sunset + offset_min),
               then turns ON all enabled zones.
  - "night_off": turns OFF all zones. Use this from cron at off_time_weekdays
                 or off_time_weekend depending on day of week.
  - "vacation_tick": when vacation_mode is enabled, randomly toggle zones
                     to simulate presence. Idempotent — safe to run hourly.

Secrets:
  SHELLY_AUTH_KEY     - Shelly cloud auth_key
  SLACK_WEBHOOK_URL   - Optional notification webhook
  CONFIG_URL          - Raw URL to config.json
  LIGHTING_MODE       - evening | night_off | vacation_tick
"""

import os
import sys
import json
import time
import random
import datetime
import urllib.parse
import urllib.request

LAT, LON = 41.5463, -8.7882
SHELLY_BASE = "https://shelly-46-eu.shelly.cloud"
AUTH_KEY = os.environ.get("SHELLY_AUTH_KEY", "").strip()
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
CONFIG_URL = os.environ.get("CONFIG_URL",
    "https://raw.githubusercontent.com/jafonsosantos/irrigation-dashboard/main/config.json")
MODE = os.environ.get("LIGHTING_MODE", "evening").strip()

LIGHT_ZONES = [
    {"name": "jardim_norte",     "label": "Jardim Norte",       "device": "c8c9a379f6f3", "channel": 0},
    {"name": "oliveira_acer",    "label": "Oliveira e Acer",    "device": "c8c9a379f6f3", "channel": 1},
    {"name": "jardim_entrada",   "label": "Jardim Entrada",     "device": "4c7525330966", "channel": 0},
    {"name": "deck_sul",         "label": "Deck Sul",           "device": "4c7525330966", "channel": 1},
    {"name": "deck_cozinha",     "label": "Deck Cozinha",       "device": "fcb467329c28", "channel": 0},
    {"name": "deck_sala_jantar", "label": "Deck Sala Jantar",   "device": "fcb467329c28", "channel": 1},
    {"name": "fachada_frente",   "label": "Fachada Frente",     "device": "34945471e686", "channel": 0},
]

def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "smart-lighting/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def http_post_form(url, data, timeout=20):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                  headers={"Content-Type": "application/x-www-form-urlencoded",
                                           "User-Agent": "smart-lighting/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def load_config():
    try:
        cfg = http_get(CONFIG_URL)
        lighting = cfg.get("lighting", {})
        lighting.setdefault("sunset_offset_min", 0)
        lighting.setdefault("off_time_weekdays", "01:00")
        lighting.setdefault("off_time_weekend", "02:30")
        lighting.setdefault("weekend_days", [5, 6])
        lighting.setdefault("vacation_mode", False)
        lighting.setdefault("zones", {})
        for z in LIGHT_ZONES:
            lighting["zones"].setdefault(z["name"], {"enabled": True})
        return lighting
    except Exception as e:
        print(f"WARN: config load failed ({e}) — using defaults")
        return {
            "sunset_offset_min": 0,
            "off_time_weekdays": "01:00",
            "off_time_weekend": "02:30",
            "weekend_days": [5, 6],
            "vacation_mode": False,
            "zones": {z["name"]: {"enabled": True} for z in LIGHT_ZONES},
        }

def fetch_sunset_today():
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}"
           "&daily=sunrise,sunset&timezone=Europe/Lisbon")
    data = http_get(url)
    today = datetime.date.today().isoformat()
    idx = data["daily"]["time"].index(today)
    sunset_str = data["daily"]["sunset"][idx]  # "2026-06-01T21:02"
    return datetime.datetime.fromisoformat(sunset_str)

def shelly_on(device, channel, timer=None):
    data = {"auth_key": AUTH_KEY, "id": device, "channel": channel, "turn": "on"}
    if timer:
        data["timer"] = timer
    return http_post_form(f"{SHELLY_BASE}/device/relay/control", data)

def shelly_off(device, channel):
    data = {"auth_key": AUTH_KEY, "id": device, "channel": channel, "turn": "off"}
    return http_post_form(f"{SHELLY_BASE}/device/relay/control", data)

def turn_zone(zone, on):
    try:
        if on:
            shelly_on(zone["device"], zone["channel"])
        else:
            shelly_off(zone["device"], zone["channel"])
        print(f"  {'ON' if on else 'OFF'} {zone['label']}")
        return True
    except Exception as e:
        print(f"  ERROR {zone['label']}: {e}")
        return False

def slack_notify(text):
    if not SLACK_WEBHOOK:
        print(f"(no Slack) {text}")
        return
    body = json.dumps({"text": text}).encode()
    req = urllib.request.Request(SLACK_WEBHOOK, data=body, method="POST",
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
    except Exception as e:
        print(f"Slack error: {e}")

def lisbon_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Europe/Lisbon")).replace(tzinfo=None)
    except Exception:
        return datetime.datetime.utcnow() + datetime.timedelta(hours=1)

def mode_evening(cfg):
    """Acende as zonas se ja for pos-por-do-sol. Idempotente, SEM sleep longo, em hora de Lisboa.
    Age so na janela [por-do-sol+offset, +2h]; pensado para disparos frequentes (cron + gatilho externo).
    Corrige: comparava por-do-sol (hora de Lisboa) com now() do runner (UTC) e dormia ~2h ate expirar o job."""
    try:
        sunset = fetch_sunset_today()            # naive, hora de Lisboa
    except Exception as e:
        print(f"sunset fetch failed: {e} - fallback 21:00")
        sunset = datetime.datetime.combine(datetime.date.today(), datetime.time(21, 0))
    offset_min = int(cfg.get("sunset_offset_min", 0))
    trigger = sunset + datetime.timedelta(minutes=offset_min)
    now = lisbon_now()
    window_end = trigger + datetime.timedelta(minutes=120)
    print(f"Sunset: {sunset} | Trigger: {trigger} | Agora (Lisboa): {now}")
    if now < trigger:
        print("Ainda nao e por-do-sol - nada a fazer (disparo cedo).")
        return
    if now > window_end:
        print("Fora da janela (>2h apos por-do-sol) - nada a fazer.")
        return
    enabled = [z for z in LIGHT_ZONES if cfg["zones"].get(z["name"], {}).get("enabled", True)]
    print(f"Pos-por-do-sol - a garantir ON em {len(enabled)} zonas...")
    on_count = 0
    for zone in enabled:
        if turn_zone(zone, True):
            on_count += 1
        time.sleep(0.5)
    slack_notify(f"Luzes ON ({on_count}/{len(enabled)} zonas) - por do sol {sunset.strftime('%H:%M')}")

def mode_night_off(cfg):
    """Turn OFF all lighting zones."""
    print("Turning OFF all zones...")
    off_count = 0
    for zone in LIGHT_ZONES:
        if turn_zone(zone, False):
            off_count += 1
        time.sleep(0.4)
    slack_notify(f"Luzes OFF ({off_count}/{len(LIGHT_ZONES)} zonas).")

def mode_vacation_tick(cfg):
    """Random toggle for presence simulation. Only acts if vacation_mode is on."""
    if not cfg.get("vacation_mode"):
        print("Vacation mode OFF - skipping")
        return
    enabled = [z for z in LIGHT_ZONES if cfg["zones"].get(z["name"], {}).get("enabled", True)]
    if not enabled:
        return
    # Pick 1-2 random zones, toggle them
    sample = random.sample(enabled, min(2, len(enabled)))
    action = random.choice([True, False])
    print(f"Vacation tick: {'ON' if action else 'OFF'} for {[z['name'] for z in sample]}")
    for zone in sample:
        turn_zone(zone, action)
        time.sleep(0.5)

def main():
    if not AUTH_KEY:
        print("ERROR: SHELLY_AUTH_KEY not set", file=sys.stderr)
        sys.exit(1)

    if os.path.exists("paused.flag"):
        print("PAUSED (paused.flag present) - skipping")
        slack_notify("Luzes PAUSADAS (manual).")
        return

    cfg = load_config()
    print(f"Mode: {MODE} | vacation_mode={cfg.get('vacation_mode')}")

    if MODE == "evening":
        mode_evening(cfg)
    elif MODE == "night_off":
        mode_night_off(cfg)
    elif MODE == "vacation_tick":
        mode_vacation_tick(cfg)
    else:
        print(f"Unknown mode: {MODE}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
