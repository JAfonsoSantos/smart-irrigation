#!/usr/bin/env python3
"""
Smart Irrigation Controller for Esposende, PT.
v2 — reads config from irrigation-dashboard repo, uses default-only durations
     (no EXTENDED, no seasonal multipliers). Zone order: Norte > Oliveira > Cozinha > Entrada.

Decisions:
  - SKIP:    rain_48h > 5mm OR (forecast_prob > 70% AND rain_forecast > 3mm)
             OU solo ainda humido: et0 acumulado desde a ultima rega/chuva forte
             < et0_threshold_mm (config.json, por defeito 8mm). Auto-sazonal:
             em julho (et0 ~5mm/dia) rega ~diario; em outubro (~2mm/dia) cada 3-4 dias.
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
SHELLY_BASE = os.environ.get("SHELLY_BASE", "https://shelly-287-eu.shelly.cloud").rstrip("/")  # conta migrada de shelly-46-eu a 27/8/2026
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
WINTER_MONTHS = (11, 12, 1, 2, 3)   # novembro a marco
WINTER_MIN_DRY_DAYS = 7             # rega so com MAIS de 7 dias seguidos sem chuva
WINTER_ZONE_MIN = 5                 # minutos por zona no inverno

def fetch_weather():
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}"
           "&daily=precipitation_sum,precipitation_probability_max,et0_fao_evapotranspiration"
           "&hourly=precipitation&past_days=14&forecast_days=2&timezone=Europe/Lisbon")  # 14 dias: precisa de contar >7 dias secos
    return http_get(url)

def decide(weather, last_water_date=None, et0_threshold=8.0, reset_rain_mm=5.0):
    """Decision: SKIP, REDUCED, or NORMAL. NO EXTENDED.
    Regra sazonal automatica (out 2026): so rega quando o et0 acumulado desde a
    ultima rega (last_water_date, vem do irrigation-log.json) ou desde a ultima
    chuva forte (>= reset_rain_mm num dia) atinge et0_threshold. O et0 ja reflete
    a estacao (verao ~5mm/dia, outono ~2mm/dia), por isso o intervalo entre regas
    adapta-se sozinho ao longo do ano."""
    daily = weather["daily"]
    today_idx = daily["time"].index(datetime.date.today().isoformat())

    rain_48h = sum((x or 0) for x in daily["precipitation_sum"][today_idx-2:today_idx])
    rain_forecast_12h = (daily["precipitation_sum"][today_idx] or 0) * 0.5
    forecast_probability = daily["precipitation_probability_max"][today_idx] or 0
    et0_today = daily["et0_fao_evapotranspiration"][today_idx] or 0

    dry_days = 0
    for i in range(today_idx - 1, -1, -1):
        if (daily["precipitation_sum"][i] or 0) < 1:
            dry_days += 1
        else:
            break

    # et0 acumulado desde o ultimo "reset" (rega registada no log OU dia com
    # chuva >= reset_rain_mm). Se a ultima rega for mais antiga que a janela de
    # past_days do open-meteo, acumula a janela toda (garante que nunca fica
    # semanas sem regar por falta de historico).
    et0_accum = None
    try:
        start_i = 0
        if last_water_date and last_water_date in daily["time"]:
            start_i = daily["time"].index(last_water_date) + 1
        for i in range(today_idx - 1, start_i - 1, -1):
            if (daily["precipitation_sum"][i] or 0) >= reset_rain_mm:
                start_i = i + 1
                break
        et0_accum = sum((daily["et0_fao_evapotranspiration"][i] or 0)
                        for i in range(start_i, today_idx + 1))
    except Exception as e:
        print(f"  et0_accum calc error: {e}")
        et0_accum = None

    skip_reason = ""
    # REGRA DE INVERNO (Catarina, 26/11/2026): Esposende e humido, o orvalho chega.
    # Nov-Mar: nenhuma rega se choveu (>=1mm em 48h) ou se vai chover hoje;
    # so rega com MAIS de 7 dias seguidos sem chuva, e so 5 min por zona.
    if datetime.date.today().month in WINTER_MONTHS:
        rain_today = daily["precipitation_sum"][today_idx] or 0
        if rain_48h >= 1 or (rain_today >= 1 and forecast_probability >= 50):
            decision, factor, skip_reason = "SKIP", 0.0, "chuva"
        elif dry_days <= WINTER_MIN_DRY_DAYS:
            decision, factor, skip_reason = "SKIP", 0.0, "inverno"
        else:
            decision, factor, skip_reason = "NORMAL", 1.0, "inverno_rega"
        return {
            "decision": decision, "factor": factor, "skip_reason": skip_reason,
            "rain_48h": round(rain_48h, 2), "rain_forecast_12h": round(rain_forecast_12h, 2),
            "forecast_probability": int(forecast_probability), "dry_days": dry_days,
            "et0": round(et0_today, 2),
            "et0_accum": round(et0_accum, 2) if et0_accum is not None else None,
            "et0_threshold": et0_threshold,
            "fixed_min": WINTER_ZONE_MIN if decision == "NORMAL" else None,
        }
    if rain_48h > 5 or (forecast_probability > 70 and rain_forecast_12h > 3):
        decision = "SKIP"
        factor = 0.0
        skip_reason = "chuva"
    elif et0_accum is not None and et0_accum < et0_threshold:
        decision = "SKIP"
        factor = 0.0
        skip_reason = "solo"
    elif 2 <= rain_48h <= 5:
        decision = "REDUCED"
        factor = 0.5
    else:
        decision = "NORMAL"
        factor = 1.0

    return {
        "decision": decision,
        "factor": factor,
        "skip_reason": skip_reason,
        "rain_48h": round(rain_48h, 2),
        "rain_forecast_12h": round(rain_forecast_12h, 2),
        "forecast_probability": int(forecast_probability),
        "dry_days": dry_days,
        "et0": round(et0_today, 2),
        "et0_accum": round(et0_accum, 2) if et0_accum is not None else None,
        "et0_threshold": et0_threshold,
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

def email_notify(subject, body):
    """Envia email se as variaveis SMTP_* estiverem definidas (via GitHub Secrets)."""
    host = os.environ.get("SMTP_HOST", "").strip()
    user = os.environ.get("SMTP_USER", "").strip()
    pw   = os.environ.get("SMTP_PASS", "").strip()
    to   = os.environ.get("EMAIL_TO", "").strip() or user
    try:
        port = int(os.environ.get("SMTP_PORT", "587") or "587")
    except Exception:
        port = 587
    if not (host and user and pw and to):
        print("(no SMTP_* env set, skipping email)")
        return
    try:
        import smtplib
        from email.mime.text import MIMEText
        m = MIMEText(body, "plain", "utf-8")
        m["Subject"] = subject
        m["From"] = user
        rcpts = [a.strip() for a in to.replace(";", ",").split(",") if a.strip()]
        m["To"] = ", ".join(rcpts)
        with smtplib.SMTP(host, port, timeout=20) as srv:
            srv.starttls()
            srv.login(user, pw)
            srv.sendmail(user, rcpts, m.as_string())
        print(f"Email sent to {', '.join(rcpts)}")
    except Exception as e:
        print(f"Email notify error: {e}")

def lisbon_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Europe/Lisbon"))
    except Exception:
        return datetime.datetime.utcnow() + datetime.timedelta(hours=1)

def notify(text, subject=None):
    """Notifica via Slack (se configurado) E email (se configurado)."""
    slack_notify(text)
    t = text.lower()
    # Email so quando interessa (nov 2026): quando REGA ou quando ha ATENCAO/falha.
    # Saltos de rotina (chuva, solo humido, cron atrasado, pausa) nao enviam email.
    routine = any(k in t for k in ("saltada por chuva", "solo ainda", "fora da janela",
                                   "ja correu", "ja regou", "paused", "pausada"))
    if routine and "atencao" not in t:
        print("(skip de rotina - sem email)")
        return
    if subject is None:
        if "atencao" in t or "warn" in t or "falh" in t:
            subject = "Rega Esposende - ATENCAO"
        else:
            subject = "Rega Esposende - regou"
    email_notify(subject, text)

# -----------------------------------------------------------------------------
# Email diario (nov 2026): "HOJE REGOU" / "HOJE NAO REGOU" + motivo + dados
# -----------------------------------------------------------------------------
LITROS_POR_MIN = 43.0   # estimado da fatura set->out 2026 (22 m3 em 512 min de rega)
EURO_POR_M3 = 2.92      # custo marginal Esposende Ambiente (agua 2o escalao+saneamento+residuos+taxas)
DASHBOARD_URL = "https://jafonsosantos.github.io/irrigation-dashboard/"

def _fmt(v, unit=""):
    if v is None or v == -1:
        return "sem dados"
    return f"{v}{unit}"

def build_daily_email(plan, zones_detail, total_minutes, start, teste=False):
    regou = plan["decision"] in ("NORMAL", "REDUCED") and any(z.get("completed") for z in zones_detail)
    subject = "HOJE REGOU" if regou else "HOJE NAO REGOU"
    hoje = lisbon_now().strftime("%d/%m/%Y %H:%M")
    reason = plan.get("skip_reason", "")
    if plan["decision"] == "PAUSED":
        motivo = "Rega em PAUSA (botao Pausar no dashboard). Nenhuma zona foi ligada."
    elif plan["decision"] == "SKIP" and reason == "sem_meteo":
        motivo = ("ATENCAO: nao foi possivel obter a meteorologia (5 tentativas em 5 min falharam). "
                  "Por seguranca, sem dados o sistema NAO rega.")
    elif plan["decision"] == "SKIP" and reason == "inverno":
        motivo = (f"Regra de inverno (nov-mar): so rega com mais de {WINTER_MIN_DRY_DAYS} dias seguidos sem chuva. "
                  f"Hoje: {_fmt(plan.get('dry_days'))} dias sem chuva.")
    elif plan["decision"] == "NORMAL" and reason == "inverno_rega":
        motivo = (f"Regra de inverno: {_fmt(plan.get('dry_days'))} dias seguidos sem chuva (mais de "
                  f"{WINTER_MIN_DRY_DAYS}) -> rega curta de {WINTER_ZONE_MIN} min por zona.")
    elif plan["decision"] == "SKIP" and reason == "solo":
        motivo = (f"Solo ainda com agua: evaporacao acumulada desde a ultima rega/chuva = "
                  f"{_fmt(plan.get('et0_accum'), ' mm')}, abaixo do limite de {_fmt(plan.get('et0_threshold'), ' mm')}.")
    elif plan["decision"] == "SKIP":
        inverno = lisbon_now().month in WINTER_MONTHS
        limite = 1 if inverno else 5
        if isinstance(plan.get("rain_48h"), (int, float)) and plan["rain_48h"] >= limite:
            motivo = (f"Choveu {_fmt(plan['rain_48h'], ' mm')} nas ultimas 48h "
                      + ("(inverno: qualquer chuva >= 1 mm cancela a rega)." if inverno else "(rega so abaixo de 5 mm)."))
        else:
            motivo = (f"Chuva prevista para hoje: {_fmt(plan['rain_forecast_12h'], ' mm')} "
                      f"com {_fmt(plan['forecast_probability'], '%')} de probabilidade.")
    elif plan["decision"] == "REDUCED":
        motivo = f"Chuva fraca nas ultimas 48h ({_fmt(plan['rain_48h'], ' mm')}): meia rega."
    else:
        motivo = (f"Tempo seco: {_fmt(plan['rain_48h'], ' mm')} de chuva em 48h, "
                  f"{_fmt(plan.get('dry_days'))} dias sem chuva, solo precisa de agua.")
    falhas = [z["name"] for z in zones_detail if not z.get("completed")]
    if plan["decision"] in ("NORMAL", "REDUCED") and falhas:
        motivo = (f"ATENCAO: {len(falhas)} zona(s) nao confirmaram ligar/desligar ({', '.join(falhas)}). "
                  f"Verificar Shelly. | " + motivo)

    linhas = []
    if teste:
        linhas += ["*** EMAIL DE TESTE - nenhuma zona foi ligada ***", ""]
    linhas += [f"{subject} - {hoje}", "", f"MOTIVO: {motivo}", "", "DADOS METEO (Esposende, open-meteo):",
               f"- Chuva ultimas 48h: {_fmt(plan['rain_48h'], ' mm')}",
               f"- Chuva prevista hoje (12h): {_fmt(plan['rain_forecast_12h'], ' mm')}",
               f"- Probabilidade de chuva hoje: {_fmt(plan['forecast_probability'], '%')}",
               f"- Dias seguidos sem chuva: {_fmt(plan.get('dry_days'))}",
               f"- Evaporacao (ET0) hoje: {_fmt(plan.get('et0'), ' mm')}",
               f"- ET0 acumulada desde ultima rega/chuva: {_fmt(plan.get('et0_accum'), ' mm')} "
               f"(rega a partir de {_fmt(plan.get('et0_threshold'), ' mm')})", ""]
    if zones_detail:
        linhas.append("ZONAS:")
        for z in zones_detail:
            ok = "OK" if z.get("completed") else "FALHOU"
            linhas.append(f"- {z['name'].capitalize()}: {z['duration_min']} min [{ok}]")
        litros = round(total_minutes * LITROS_POR_MIN)
        linhas += [f"Total: {round(total_minutes)} min | ~{litros} litros | ~{round(litros/1000*EURO_POR_M3, 2)} EUR (estimativa)", ""]
    else:
        linhas += ["ZONAS: nenhuma ligada | 0 litros | 0 EUR", ""]
    linhas += [f"Proxima avaliacao: amanha as {start} (Lisboa).", f"Dashboard: {DASHBOARD_URL}"]
    return subject, "\n".join(linhas)

def send_test_emails(config):
    """Disparo manual com reason=teste_email: envia os 2 formatos com dados reais de hoje, sem ligar zonas."""
    start = config.get("start_time", "04:00")
    try:
        plan = decide(fetch_weather(), None)
    except Exception as e:
        print(f"meteo falhou no teste: {e}")
        plan = {"decision": "SKIP", "factor": 0.0, "skip_reason": "sem_meteo", "rain_48h": -1,
                "rain_forecast_12h": -1, "forecast_probability": -1, "dry_days": -1, "et0": -1,
                "et0_accum": None, "et0_threshold": 8.0}
    fake = [{"name": z["name"], "channel": z["channel"],
             "duration_min": float(config["zones"].get(z["name"], 10)), "completed": True} for z in ZONES]
    # exemplo REGOU com dados de um dia seco real (14 out 2026), para o teste fazer sentido
    regou_plan = {"decision": "NORMAL", "factor": 1.0, "skip_reason": "", "rain_48h": 0.0,
                  "rain_forecast_12h": 0.0, "forecast_probability": 0, "dry_days": 7, "et0": 2.1,
                  "et0_accum": 9.4, "et0_threshold": 8.0}
    s1, b1 = build_daily_email(regou_plan, fake, sum(z["duration_min"] for z in fake), start, teste=True)
    nao_plan = plan if plan["decision"] == "SKIP" else dict(plan, decision="SKIP", skip_reason="chuva")
    s2, b2 = build_daily_email(nao_plan, [], 0, start, teste=True)
    for s, b in ((s1, b1), (s2, b2)):
        print(f"--- {s} ---\n{b}\n")
        email_notify(s, b)

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main():
    if not AUTH_KEY:
        print("ERROR: SHELLY_AUTH_KEY not set", file=sys.stderr)
        sys.exit(1)

    event = os.environ.get("GITHUB_EVENT_NAME", "")
    dispatch_reason = os.environ.get("DISPATCH_REASON", "").strip()
    # Manual = botao do dashboard (ignora janela+idempotencia). Gatilho externo manda
    # inputs.reason="scheduler" e e tratado como agendado (com janela + idempotencia).
    is_manual = (event == "workflow_dispatch" and dispatch_reason != "scheduler")

    if dispatch_reason == "teste_email":
        send_test_emails(load_config())
        return

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
        if dispatch_reason == "scheduler":   # so o disparo das 04:00 manda email (evita duplicados)
            try:
                st = load_config().get("start_time", "04:00")
            except Exception:
                st = "04:00"
            subj, body = build_daily_email({"decision": "PAUSED", "rain_48h": -1, "rain_forecast_12h": -1,
                                            "forecast_probability": -1, "dry_days": -1, "et0": -1,
                                            "et0_accum": None, "et0_threshold": None}, [], 0, st)
            email_notify(subj, body)
        return

    # Carregar config cedo (preciso do start_time para a janela horaria)
    config = load_config()
    print(f"Config loaded: zones={config['zones']} start_time={config.get('start_time')}")

    # Time-window guard: o cron do GitHub Actions e best-effort e atrasa-se HORAS.
    # A janela acompanha automaticamente a hora de inicio (config.start_time) + uma margem.
    # margem configuravel em config.json ("cron_window_margin_min", por defeito 120 = 2h).
    # Execucoes manuais (workflow_dispatch) ignoram isto. Evita rega fora de horas.
    if not is_manual:
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

    # Ultima rega registada no log (para a regra do et0 acumulado)
    last_water_date = None
    try:
        with open(LOG_PATH) as f:
            for e in json.load(f):
                if e.get("decision") in ("NORMAL", "REDUCED") and (e.get("zones_watered") or []):
                    d = e.get("date")
                    if d and (last_water_date is None or d > last_water_date):
                        last_water_date = d
    except Exception:
        pass
    try:
        et0_threshold = max(3.0, min(20.0, float(config.get("et0_threshold_mm", 8))))
    except Exception:
        et0_threshold = 8.0
    try:
        reset_rain_mm = max(1.0, min(20.0, float(config.get("et0_reset_rain_mm", 5))))
    except Exception:
        reset_rain_mm = 5.0

    # FAIL-SAFE (nov 2026): sem dados meteo NAO rega. Antes caia em NORMAL e regou
    # 5 dias em novembro no meio de ~460mm de chuva. Tenta 5x (1 min entre tentativas) antes de desistir.
    plan = None
    last_err = None
    for attempt in range(1, 6):
        try:
            weather = fetch_weather()
            plan = decide(weather, last_water_date, et0_threshold, reset_rain_mm)
            break
        except Exception as e:
            last_err = e
            print(f"Weather/decision attempt {attempt} failed: {e}")
            if attempt < 5:
                time.sleep(60)  # open-meteo devolve 429 aos IPs partilhados do GitHub
    if plan is None:
        print(f"Weather unavailable ({last_err}) — SKIP por seguranca (nao rega sem saber se choveu)")
        plan = {"decision": "SKIP", "factor": 0.0, "skip_reason": "sem_meteo",
                "rain_48h": -1, "rain_forecast_12h": -1, "forecast_probability": -1,
                "dry_days": -1, "et0": -1, "et0_accum": None, "et0_threshold": et0_threshold}

    print(f"Decision: {plan['decision']} (factor={plan['factor']})")
    print(f"  rain_48h={plan['rain_48h']}mm  forecast_12h={plan['rain_forecast_12h']}mm "
          f"prob={plan['forecast_probability']}%  dry_days={plan['dry_days']}  et0={plan['et0']}mm")
    print(f"  et0_accum={plan.get('et0_accum')}mm / threshold={plan.get('et0_threshold')}mm "
          f"(ultima rega: {last_water_date or 'desconhecida'})")

    zones_watered = []
    zones_detail = []
    total_minutes = 0.0

    if plan["decision"] != "SKIP":
        for zone in ZONES:
            if plan.get("fixed_min"):
                dur_sec = int(plan["fixed_min"] * 60)   # regra de inverno: 5 min por zona
            else:
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
        "et0_accum_mm": plan.get("et0_accum"),
        "et0_threshold_mm": plan.get("et0_threshold"),
        "skip_reason": plan.get("skip_reason", ""),
        "zones_watered": zones_watered,
        "zones": zones_detail,
        "total_duration_min": round(total_minutes, 1),
        "config_used": {"zones": config["zones"], "start_time": config.get("start_time")},
    }
    append_log(entry)
    print(f"\nLogged to {LOG_PATH}")

    hora = lisbon_now().strftime("%H:%M")
    start = config.get("start_time", "04:00")
    if plan["decision"] == "SKIP" and plan.get("skip_reason") == "solo":
        msg = (f"Rega SALTADA - solo ainda com agua (et0 acumulado "
               f"{plan.get('et0_accum')}mm de {plan.get('et0_threshold')}mm desde a ultima rega/chuva). "
               f"Proxima avaliacao: amanha as {start} (Lisboa).")
    elif plan["decision"] == "SKIP" and plan.get("skip_reason") == "inverno":
        msg = f"Rega SALTADA - regra de inverno ({plan.get('dry_days')} dias sem chuva, minimo >{WINTER_MIN_DRY_DAYS})."
    elif plan["decision"] == "SKIP" and plan.get("skip_reason") == "sem_meteo":
        msg = (f"ATENCAO: Rega SALTADA por seguranca - nao consegui obter a meteorologia "
               f"(open-meteo falhou 5x). Sem dados, o sistema nao rega. Proxima tentativa: amanha as {start}.")
    elif plan["decision"] == "SKIP":
        msg = (f"Rega SALTADA por chuva ({plan['rain_48h']}mm nas ultimas 48h). "
               f"Proxima rega prevista: amanha as {start} (Lisboa).")
    elif plan["decision"] == "REDUCED":
        msg = (f"Rega REDUZIDA as {hora} (Lisboa) - {len(zones_watered)} zonas, ~{round(total_minutes)}min "
               f"(meia rega; choveu {plan['rain_48h']}mm em 48h).")
    else:
        msg = (f"Rega NORMAL as {hora} (Lisboa) - {len(zones_watered)} zonas, ~{round(total_minutes)}min. "
               f"Chuva 48h: {plan['rain_48h']}mm.")
    failed = [z["name"] for z in zones_detail if not z.get("completed")]
    if failed:
        msg = (f"ATENCAO: {len(failed)} zona(s) nao confirmaram ligar/desligar: {', '.join(failed)}. "
               f"Verificar Shelly (offline? servidor mudou?). | " + msg)
    slack_notify(msg)
    subj, body = build_daily_email(plan, zones_detail, total_minutes, start)
    email_notify(subj, body)
    print(f"\nNotify: {msg}\nEmail: {subj}\n{body}")

if __name__ == "__main__":
    main()
