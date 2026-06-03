#!/usr/bin/env python3
"""
Smart Irrigation Controller for Esposende, PT.
v2 — reads config from irrigation-dashboard repo, uses default-only durations
     (no EXTENDED, no seasonal multipliers). Zone order: Norte > Oliveira > Cozinha > Entrada.

Decisions:
  - SKIP:    rain_48h > 5mm OR (forecast_prob > 70% AND rain_forecast > 3mm)
  - REDUCED: rain_48h 2-5mm  -> water at 50% of default duration (channel 0 only;
                                channel 1 hardware-locked at 15min)
  - NORMAL:  default per-zone duration from config.json (1-20min each, hard cap)

Secrets (env):
  SHELLY_AUTH_KEY     - Shelly cloud auth_key
  SLACK_WEBHOOK_URL   - Incoming webhook URL (optional)
  CONFIG_URL          - URL to config.json (defaults to irrigation-dashboard main)
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
CONFIG_URL = os.environ.get("CONFIG_URL",
    "https://raw.githubusercontent.com/jafonsosantos/irrigation-dashboard/main/config.json")

# New order: Norte -> Oliveira -> Cozinha -> Entrada (clockwise)
ZONES = [
    {"name": "norte",    "device": "c8c9a379ff9d", "channel": 1, "use_timer": False, "fixed": False},
    {"name": "oliveira", "device": "c8c9a379ff9d", "channel": 0, "use_timer": True,  "fixed": False},
    {"name": "cozinha",  "device": "c8c9a37a09c4", "channel": 1, "use_timer": False, "fixed": False},
    {"name": "entrada",  "device": "c8c9a37a09c4", "channel": 0, "use_timer": True,  "fixed": False},
]

# Defaults if config.json fetch fails
DEFAULT_CONFIG = {
    "start_time": "06:00",
    "zones": {"norte": 15, "oliveira": 15, "cozinha": 15, "entrada": 15},
}

# -----------------------------------------------------------------------------
# HTTP helpers
# -----------------------------------------------------------------------------
def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "smart-irrigation/2.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def http_post_form(url, data, timeout=20):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                  headers={"Content-Type": "application/x-www-form-urlencoded",
                                           "User-Agent": "smart-irrigation/2.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

# -----------------------------------------------------------------------------
# Load config from dashboard repo
# -----------------------------------------------------------------------------
def load_config():
    try:
        cfg = http_get(CONFIG_URL)
        # Sanity check + clamp
        zones = cfg.get("zones", {})
        for name in ("norte", "oliveira", "cozinha", "entrada"):
            v = zones.get(name, 15)
            try:
                v = int(v)
            except Exception:
                v = 15
            v = max(1, min(20, v))
            zones[name] = v
        cfg["zones"] = zones
        cfg.setdefault("start_time", "06:00")
        return cfg
    except Exception as e:
        print(f"WARN: could not load config ({e}) — using defaults")
        return dict(DEFAULT_CONFIG)

# -----------------------------------------------------------------------------
# Weather + decision
# -----------------------------------------------------------------------------
def fetch_weather():
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}"
           "&daily=precipitation_sum,precipitation_probability_max,et0_fao_evapotranspiration"
           "&hourly=precipitation&past_days=7&forecast_days=2&timezone=Europe/Lisbon")
    return http_get(url)

def decide(weather):
    """Decision: SKIP, REDUCED, or NORMAL. NO EXTENDED. NO seasonal multipliers."""
    daily = weather["daily"]
    today_idx = daily["time"].index(datetime.date.today().isoformat())

    rain_48h = sum(daily["precipitation_sum"][today_idx-2:today_idx])
    rain_forecast_12h = (daily["precipitation_sum"][today_idx] or 0) * 0.5
    forecast_probability = daily["precipitation_probability_max"][today_idx] or 0
    et0_today = daily["et0_fao_evapotranspiration"][today_idx] or 0

    dry_days = 0
    for i in range(today_idx - 1, -1, -1):
        if (daily["precipitation_sum"][i] or 0) < 1:
            dry_days += 1
        else:
            break

    if rain_48h > 5 or (forecast_probability > 70 and rain_forecast_12h > 3):
        decision = "SKIP"
        factor = 0.0
    elif 2 <= rain_48h <= 5:
        decision = "REDUCED"
        factor = 0.5
    else:
        decision = "NORMAL"
        factor = 1.0

    return {
        "decision": decision,
        "factor": factor,
        "rain_48h": round(rain_48h, 2),
        "rain_forecast_12h": round(rain_forecast_12h, 2),
        "forecast_probability": int(forecast_probability),
        "dry_days": dry_days,
        "et0": round(et0_today, 2),
    }

def zone_duration_sec(zone, config, factor):
    """Return duration in seconds for a zone based on config + factor.
    Channel 1 (fixed=True) zones always use 900s (hardware-locked auto-off).
    Channel 0 zones use config.zones[name] * factor, clamped 60..1500 (=25min)."""
    if zone["fixed"]:
        return 900
    base_min = config["zones"].get(zone["name"], 15)
    sec = int(base_min * 60 * factor)
    return max(60, min(sec, 1200))  # 1min..20min safety (teto de seguranca de hardware)

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

def run_zone(zone, dur_sec):
    name = zone["name"]
    device = zone["device"]
    channel = zone["channel"]
    use_timer = zone["use_timer"]
    duration = dur_sec

    print(f"\n--- Zone: {name} (device {device} ch{channel}, {duration}s) ---")
    try:
        # Failsafe: envia SEMPRE o timer de hardware (auto-off) nas 4 zonas, incluindo
        # as que nao usavam timer, para o rele desligar sozinho se o script/rede falharem.
        shelly_on(device, channel, timer=duration)
    except Exception as e:
        print(f"  ON error: {e}")
        return False

    time.sleep(3)
    state = get_relay_state(device, channel)
    print(f"  ON verify: ison={state}")
    if state is not True:
        print(f"  WARN: zone {name} did not turn on")
        return False

    time.sleep(duration)

    ok = turn_off_with_retry(device, channel, name)
    if not ok:
        print(f"  WARN: zone {name} could not be confirmed OFF")
    time.sleep(10)
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

    event = os.environ.get("GITHUB_EVENT_NAME", "")
    is_manual = (event == "workflow_dispatch")  # "Disparar run ja" ignora janela + idempotencia

    if os.path.exists("paused.flag"):
        msg = "Irrigation PAUSED via dashboard (paused.flag present). Skipping run."
        print(msg)
        now = datetime.datetime.now()
        append_log({
            "date": now.date().isoformat(),
            "time": now.strftime("%H:%M"),
            "decision": "PAUSED",
            "reason": "paused.flag present (manual pause via dashboard)",
            "zones_watered": [],
            "total_duration_min": 0,
        })
        slack_notify("Rega PAUSADA (manual via dashboard).")
        return

    # Carregar config cedo (preciso do start_time para a janela horaria)
    config = load_config()
    print(f"Config loaded: zones={config['zones']} start_time={config.get('start_time')}")

    # Time-window guard: o cron do GitHub Actions e best-effort e atrasa-se HORAS.
    # A janela acompanha automaticamente a hora de inicio (config.start_time) + uma margem.
    # margem configuravel em config.json ("cron_window_margin_min", por defeito 120 = 2h).
    # Execucoes manuais (workflow_dispatch) ignoram isto. Evita rega fora de horas.
    if event == "schedule":
        try:
            margin_min = int(config.get("cron_window_margin_min", 120))
        except Exception:
            margin_min = 120
        try:
            from zoneinfo import ZoneInfo
            now_lis = datetime.datetime.now(ZoneInfo("Europe/Lisbon"))
        except Exception:
            now_lis = datetime.datetime.utcnow() + datetime.timedelta(hours=1)  # assume DST +1
        try:
            sh, sm = [int(x) for x in str(config.get("start_time", "04:00")).split(":")[:2]]
        except Exception:
            sh, sm = 4, 0
        start_min = sh * 60 + sm
        now_min = now_lis.hour * 60 + now_lis.minute
        lo = start_min - 10          # tolerancia de 10 min antes (skew)
        hi = start_min + margin_min  # margem para o atraso do GitHub
        in_window = (lo <= now_min <= hi) or (lo <= now_min + 1440 <= hi)  # trata passagem da meia-noite
        if not in_window:
            hi_h, hi_m = (hi // 60) % 24, hi % 60
            win_desc = f"{sh:02d}:{sm:02d}-{hi_h:02d}:{hi_m:02d} Lisboa (inicio +{margin_min}min)"
            now = datetime.datetime.now()
            msg = (f"Rega SALTADA - fora da janela ({win_desc}). Cron disparou as "
                   f"{now_lis.hour:02d}:{now_lis.minute:02d} (Lisboa). Nao regou para evitar rega fora de horas.")
            print(msg)
            append_log({
                "date": now.date().isoformat(),
                "time": now.strftime("%H:%M"),
                "decision": "SKIP",
                "reason": f"fora da janela de rega ({win_desc}) - cron atrasado pelo GitHub Actions",
                "zones_watered": [],
                "total_duration_min": 0,
            })
            slack_notify(msg)
            return

    # Idempotency check (DOUBLE GUARD): check both the log file AND GitHub Actions API.
    # The log file can be stale if a previous run failed to push, so we also query the API
    # which is the source of truth for "did a run happen today".
    today_iso = datetime.date.today().isoformat()
    current_run_id = os.environ.get("GITHUB_RUN_ID", "")
    # Guard A: check log file (fast path)
    log_already_ran = False
    try:
        with open(LOG_PATH) as f:
            existing = json.load(f)
        for e in existing:
            if e.get("date") == today_iso and e.get("decision") in ("NORMAL", "REDUCED") and (e.get("zones_watered") or []) and not is_manual:
                log_already_ran = True
                print(f"LOG idempotency: already ran today ({e.get('time')}, {e.get('decision')})")
                break
            if e.get("date") == today_iso and e.get("decision") == "SKIP" and not is_manual:
                print(f"LOG idempotency: today SKIPPED ({e.get('time')})")
                return
    except (FileNotFoundError, Exception) as ex:
        if not isinstance(ex, FileNotFoundError):
            print(f"Log check error: {ex}")
    # Guard B: check GitHub Actions API for any previous successful run of THIS workflow today
    try:
        gh_token = os.environ.get("GITHUB_TOKEN", "")
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        if gh_token and repo and not is_manual:
            url = f"https://api.github.com/repos/{repo}/actions/workflows/irrigation.yml/runs?per_page=20"
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {gh_token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "smart-irrigation/2.1"
            })
            with urllib.request.urlopen(req, timeout=15) as r:
                runs = json.loads(r.read().decode()).get("workflow_runs", [])
            for r in runs:
                if r.get("id") == int(current_run_id or 0):
                    continue  # skip self
                started = r.get("run_started_at", "")
                if started.startswith(today_iso) and r.get("status") in ("in_progress", "queued", "completed") and r.get("conclusion") in (None, "success"):
                    print(f"API idempotency: run #{r.get('run_number')} ({r.get('status')}/{r.get('conclusion')}) already ran today at {started}")
                    slack_notify(f"Rega SKIP - ja correu hoje (run #{r.get('run_number')}).")
                    return
    except Exception as ex:
        print(f"API idempotency check error: {ex}")
    if log_already_ran:
        slack_notify(f"Rega SKIP - log indica que ja regou hoje.")
        return

    try:
        weather = fetch_weather()
        plan = decide(weather)
    except Exception as e:
        print(f"Weather fetch failed ({e}) — defaulting to NORMAL")
        plan = {"decision": "NORMAL", "factor": 1.0,
                "rain_48h": -1, "rain_forecast_12h": -1, "forecast_probability": -1,
                "dry_days": -1, "et0": -1}

    print(f"Decision: {plan['decision']} (factor={plan['factor']})")
    print(f"  rain_48h={plan['rain_48h']}mm  forecast_12h={plan['rain_forecast_12h']}mm "
          f"prob={plan['forecast_probability']}%  dry_days={plan['dry_days']}  et0={plan['et0']}mm")

    zones_watered = []
    zones_detail = []
    total_minutes = 0.0

    if plan["decision"] != "SKIP":
        for zone in ZONES:
            dur_sec = zone_duration_sec(zone, config, plan["factor"])
            dur_min = round(dur_sec / 60.0, 1)
            ok = run_zone(zone, dur_sec)
            zones_watered.append(zone["name"])
            zones_detail.append({
                "name": zone["name"],
                "channel": zone["channel"],
                "duration_min": dur_min,
                "completed": bool(ok),
            })
            total_minutes += dur_min

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

    now = datetime.datetime.now()
    entry = {
        "date": now.date().isoformat(),
        "time": now.strftime("%H:%M"),
        "utc_time": datetime.datetime.utcnow().strftime("%H:%M"),
        "decision": plan["decision"],
        "rain_48h_mm": plan["rain_48h"],
        "forecast_12h_mm": plan["rain_forecast_12h"],
        "forecast_prob_pct": plan["forecast_probability"],
        "dry_days": plan["dry_days"],
        "et0_mm": plan["et0"],
        "zones_watered": zones_watered,
        "zones": zones_detail,
        "total_duration_min": round(total_minutes, 1),
        "config_used": {"zones": config["zones"], "start_time": config.get("start_time")},
    }
    append_log(entry)
    print(f"\nLogged to {LOG_PATH}")

    if plan["decision"] == "SKIP":
        msg = f"Rega SKIP - {plan['rain_48h']}mm nas ultimas 48h."
    elif plan["decision"] == "REDUCED":
        msg = (f"Rega REDUZIDA - {len(zones_watered)} zonas (~{round(total_minutes)}min total, "
               f"meia rega - choveu {plan['rain_48h']}mm).")
    else:
        msg = (f"Rega NORMAL - {len(zones_watered)} zonas concluidas, total ~{round(total_minutes)}min. "
               f"Chuva 48h: {plan['rain_48h']}mm.")
    slack_notify(msg)
    print(f"\nSlack: {msg}")

if __name__ == "__main__":
    main()
