#!/usr/bin/env python3
"""
Smart Roller Blinds (Brisas) Controller for Esposende, PT.
Runs every 15min via cron. For each brisa, checks if it should open/close NOW (within +/-7min).
Uses Shelly Cloud API v2 cover endpoint.
Rest days (sabado, domingo e feriados nacionais PT): aberturas suspensas; fechos mantem-se.

Config schema (per brisa):
  open:  { mode: "fixed"|"sunrise"|"disabled", time: "HH:MM", offset: minutes }
  close: { mode: "fixed"|"sunset"|"disabled",  time: "HH:MM", offset: minutes }
"""
import os, sys, json, time, datetime, urllib.parse, urllib.request

LAT, LON = 41.5463, -8.7882
SHELLY_BASE = "https://shelly-46-eu.shelly.cloud"
AUTH_KEY = os.environ.get("SHELLY_AUTH_KEY", "").strip()
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
CONFIG_URL = os.environ.get("CONFIG_URL",
    "https://raw.githubusercontent.com/jafonsosantos/irrigation-dashboard/main/config.json")

BRISA_ZONES = [
    {"name": "oliveira",      "label": "Oliveira",          "device": "4c7525341b1f", "channel": 0, "floor": "rc"},
    {"name": "lavandaria",    "label": "Lavandaria",        "device": "3494546b963c", "channel": 0, "floor": "rc"},
    {"name": "salamandra",    "label": "Salamandra",        "device": "c8c9a367e0f9", "channel": 0, "floor": "rc"},
    {"name": "sala_estar",    "label": "Sala de estar",     "device": "c8c9a367de4d", "channel": 0, "floor": "rc"},
    {"name": "sala_jantar",   "label": "Sala de jantar",    "device": "c8c9a379faf8", "channel": 0, "floor": "rc"},
    {"name": "cozinha",       "label": "Cozinha",           "device": "3494546bc56d", "channel": 0, "floor": "rc"},
    {"name": "deck_sala",     "label": "Deck Sala",         "device": "c8c9a379fa11", "channel": 0, "floor": "rc"},
    {"name": "corredor",      "label": "Corredor",          "device": "c8c9a367df5d", "channel": 0, "floor": "cima"},
    {"name": "wc_principal",  "label": "WC principal",      "device": "3494546bc573", "channel": 0, "floor": "cima"},
    {"name": "quarto_mercedes", "label": "Quarto Mercedes", "device": "4c752534a997", "channel": 0, "floor": "cima"},
    {"name": "wc_meninas",    "label": "WC meninas",        "device": "c8c9a367e118", "channel": 0, "floor": "cima"},
    {"name": "blackout_principal", "label": "Blackout principal", "device": "4c752533c236", "channel": 0, "floor": "cima"},
    {"name": "quarto_visitas_frente", "label": "Quarto visitas frente", "device": "4c752533cc4d", "channel": 0, "floor": "cima"},
    {"name": "quarto_visitas_tras",   "label": "Quarto visitas tras",   "device": "4c752533f903", "channel": 0, "floor": "cima"},
    {"name": "escritorio",    "label": "Escritorio",        "device": "c8c9a367e10e", "channel": 0, "floor": "cima"},
    {"name": "closet",        "label": "Closet",            "device": "4c7525341a09", "channel": 0, "floor": "cima"},
]

def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "smart-brisas/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def http_post_json(url, body, timeout=20):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                  headers={"Content-Type": "application/json",
                                           "User-Agent": "smart-brisas/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()

def cover_set(zone, position):
    url = f"{SHELLY_BASE}/v2/devices/api/set/cover?auth_key={urllib.parse.quote(AUTH_KEY)}"
    return http_post_json(url, {"id": zone["device"], "channel": zone["channel"], "position": position})

def slack_notify(text):
    if not SLACK_WEBHOOK:
        print(f"(no Slack) {text}")
        return
    body = json.dumps({"text": text}).encode()
    req = urllib.request.Request(SLACK_WEBHOOK, data=body, method="POST",
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r: r.read()
    except Exception as e:
        print(f"Slack error: {e}")

def load_config():
    try:
        cfg = http_get(CONFIG_URL)
        return cfg.get("brisas", {})
    except Exception as e:
        print(f"WARN: config load failed ({e})")
        return {}

def fetch_sun_today():
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}"
           "&daily=sunrise,sunset&timezone=Europe/Lisbon")
    data = http_get(url)
    today = datetime.date.today().isoformat()
    idx = data["daily"]["time"].index(today)
    return (
        datetime.datetime.fromisoformat(data["daily"]["sunrise"][idx]),
        datetime.datetime.fromisoformat(data["daily"]["sunset"][idx]),
    )

def planned_time(spec, sunrise, sunset):
    """Returns datetime when this action should fire today, or None if disabled."""
    mode = spec.get("mode", "disabled")
    if mode == "disabled":
        return None
    offset = int(spec.get("offset", 0) or 0)
    if mode == "fixed":
        hm = (spec.get("time") or "00:00").split(":")
        return datetime.datetime.combine(datetime.date.today(), datetime.time(int(hm[0]), int(hm[1]))) + datetime.timedelta(minutes=offset)
    if mode == "sunrise":
        return sunrise + datetime.timedelta(minutes=offset)
    if mode == "sunset":
        return sunset + datetime.timedelta(minutes=offset)
    return None

def lisbon_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Europe/Lisbon")).replace(tzinfo=None)
    except Exception:
        return datetime.datetime.utcnow() + datetime.timedelta(hours=1)

def matches_now(target, window_min=7):
    if target is None: return False
    now = lisbon_now().replace(second=0, microsecond=0)
    delta = abs((target - now).total_seconds())
    return delta <= window_min * 60


def easter_sunday(year):
    """Computus (Gregoriano) — domingo de Pascoa."""
    a = year % 19; b = year // 100; c = year % 100
    d = b // 4; e = b % 4; f = (b + 8) // 25
    g = (b - f + 1) // 3; h = (19*a + b - d - g + 15) % 30
    i = c // 4; k = c % 4
    l = (32 + 2*e + 2*i - h - k) % 7
    m = (a + 11*h + 22*l) // 451
    month = (h + l - 7*m + 114) // 31
    day = ((h + l - 7*m + 114) % 31) + 1
    return datetime.date(year, month, day)

def pt_holidays(year):
    """Feriados nacionais PT (fixos + moveis)."""
    fixed = [(1,1),(4,25),(5,1),(6,10),(8,15),(10,5),(11,1),(12,1),(12,8),(12,25)]
    days = {datetime.date(year, m, d) for m, d in fixed}
    easter = easter_sunday(year)
    days.add(easter - datetime.timedelta(days=2))   # Sexta-feira Santa
    days.add(easter)                                # Pascoa
    days.add(easter + datetime.timedelta(days=60))  # Corpo de Deus
    return days

def is_rest_day(d):
    """Sabado, domingo ou feriado nacional — nao abrir brisas (dia de descanso)."""
    return d.weekday() >= 5 or d in pt_holidays(d.year)

def main():
    if not AUTH_KEY:
        print("ERROR: SHELLY_AUTH_KEY not set", file=sys.stderr); sys.exit(1)
    if os.path.exists("paused.flag"):
        print("PAUSED"); return

    brisas_cfg = load_config()
    if not brisas_cfg.get("zones"):
        print("No brisas configured"); return

    try:
        sunrise, sunset = fetch_sun_today()
    except Exception as e:
        print(f"sun fetch failed: {e}")
        sunrise = datetime.datetime.combine(datetime.date.today(), datetime.time(7, 0))
        sunset = datetime.datetime.combine(datetime.date.today(), datetime.time(20, 0))

    print(f"sunrise={sunrise.strftime('%H:%M')} sunset={sunset.strftime('%H:%M')}  now={lisbon_now().strftime('%H:%M')}")

    rest_day = is_rest_day(lisbon_now().date())
    if rest_day:
        print("Dia de descanso (sab/dom/feriado): aberturas suspensas; fechos mantem-se")

    actions = []
    for zone in BRISA_ZONES:
        bz = brisas_cfg["zones"].get(zone["name"], {})
        if not bz.get("enabled", True):
            continue
        open_t = planned_time(bz.get("open", {}), sunrise, sunset)
        close_t = planned_time(bz.get("close", {}), sunrise, sunset)

        if matches_now(open_t) and rest_day:
            print(f"  SKIP open {zone['name']} (fim de semana/feriado)")
        elif matches_now(open_t):
            try:
                cover_set(zone, "open")
                actions.append(f"{zone['label']}: OPEN")
                print(f"  OPEN {zone['name']} (target {open_t.strftime('%H:%M')})")
            except Exception as e:
                print(f"  ERROR open {zone['name']}: {e}")

        if matches_now(close_t):
            try:
                cover_set(zone, "close")
                actions.append(f"{zone['label']}: CLOSE")
                print(f"  CLOSE {zone['name']} (target {close_t.strftime('%H:%M')})")
            except Exception as e:
                print(f"  ERROR close {zone['name']}: {e}")

        time.sleep(0.5)

    if actions:
        slack_notify("Brisas: " + ", ".join(actions))
    else:
        print("No actions due this run")

if __name__ == "__main__":
    main()
