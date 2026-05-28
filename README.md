# Smart Irrigation — Esposende

Cloud-hosted cron job that waters 4 garden zones in Esposende, PT, based on weather conditions. Runs daily on GitHub Actions — no PC required.

## How it works
1. Daily at ~07:00 Lisbon, the workflow runs.
2. `irrigation.py` reads Open-Meteo weather and decides: SKIP / REDUCED / NORMAL / EXTENDED.
3. Executes 4 zones sequentially via Shelly Cloud API (oliveira → norte → entrada → cozinha).
4. Logs the run to `irrigation-log.json` (auto-committed).
5. Sends a Slack DM with the summary.

## Secrets required (Settings → Secrets and variables → Actions)
- `SHELLY_AUTH_KEY` — your Shelly cloud auth key
- `SLACK_WEBHOOK_URL` — Slack incoming webhook for DM notifications (optional)

## Manual run
Go to Actions → Smart Irrigation → Run workflow.

## Adjust schedule
Edit the `cron` line in `.github/workflows/irrigation.yml`. Format is UTC.
