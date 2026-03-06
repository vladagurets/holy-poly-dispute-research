# poly-dispute-research

Static reporter for **active Polymarket markets in UMA dispute**. The script periodically polls Polymarket’s Gamma API, filters markets that are in their **first dispute round** (exactly one “disputed” in `umaResolutionStatuses`), and posts results to a Telegram channel. It keeps local state to avoid re‑posting the same market URLs.

## What it does
1. Fetches **active markets** from Gamma API (`/markets?closed=false`) with pagination.
2. Filters to **UMA‑disputed** markets where `umaResolutionStatuses` contains **exactly one** `disputed` token.
   - Markets with 2+ dispute rounds are **ignored**.
3. Extracts **event tags** from the parent event (`/events?slug=...`) and caches them.
4. Builds a report with:
   - market URL
   - volume stats (v24h, v1w, v1m)
   - event tags
5. Sends the report to Telegram **only if at least one newly‑seen market appears**.
6. Writes state (list of URLs) **only after successful send**.

## Why it exists
- Monitor **new UMA disputes** in Polymarket quickly.
- Avoid noise by keeping state and only sending when something new appears.
- Provide quick context (volume + tags) for triage.

## Files
- `poly_dispute_report.py` — main script
- `state/state.json` — persisted list of already‑seen market URLs and latest run info
- `systemd/holy-poly-dispute-research.service` — systemd oneshot service (used by timer)
- `systemd/holy-poly-dispute-research.timer` — systemd timer (hourly)
- `install-systemd-timer.sh` — installs the timer and service
- `.env` — environment config (secrets + params); copy from `.env.example`

## Configuration (environment variables)
Required:
- `TELEGRAM_BOT_TOKEN` — Telegram bot token
- `TELEGRAM_CHAT_ID` — target chat id (e.g. `-100...` for channels)

Optional:
- `LIMIT` — page size for Gamma `/markets`; default `500`
- `STATE_PATH` — path to state file; default `./state/state.json`
- `MAX_PER_MESSAGE` — max markets per Telegram message; default `30`
- `DEBUG` — enable debug logs; `1/true/yes/on`
- `DRY_RUN` — print would‑send message to stdout, do not send; `1/true/yes/on`

## Run locally
```bash
cd /path/to/poly-dispute-research
set -a && . ./.env && set +a
python3 poly_dispute_report.py
```

Dry run (no Telegram send, no state update):
```bash
set -a && . ./.env && set +a && DRY_RUN=1 python3 poly_dispute_report.py
```

## Systemd timer (hourly, start on boot)

The script `install-systemd-timer.sh` installs a systemd **timer** that runs the report **every hour** and **starts automatically after reboot** (persistent).

### Prerequisites
- `systemd` (Linux)
- `sudo` (script copies units to `/etc/systemd/system/`)
- `.env` and `poly_dispute_report.py` in the project directory

### Install
From the project root:
```bash
./install-systemd-timer.sh
```
Or with an explicit project path:
```bash
./install-systemd-timer.sh /path/to/poly-dispute-research
```
The script will:
1. Resolve the project directory (script dir or the path you pass).
2. Check that `poly_dispute_report.py` and `.env` exist.
3. Substitute your user/group and project path into `systemd/holy-poly-dispute-research.service`.
4. Copy `holy-poly-dispute-research.service` and `holy-poly-dispute-research.timer` to `/etc/systemd/system/`.
5. Run `daemon-reload`, then `enable` and `start` the timer.

You will be prompted for your sudo password.

### After install
- **Timer status and next run:**  
  `sudo systemctl status holy-poly-dispute-research.timer`
- **List next run time:**  
  `systemctl list-timers holy-poly-dispute-research.timer`
- **Logs for the last run:**  
  `sudo journalctl -u holy-poly-dispute-research.service -n 100 --no-pager`
- **Follow logs live:**  
  `sudo journalctl -u holy-poly-dispute-research.service -f`

### Stop and remove the timer
To fully stop and remove the timer and service (e.g. before uninstalling or moving the project):
```bash
sudo systemctl stop holy-poly-dispute-research.timer
sudo systemctl disable holy-poly-dispute-research.timer
sudo rm /etc/systemd/system/holy-poly-dispute-research.service /etc/systemd/system/holy-poly-dispute-research.timer
sudo systemctl daemon-reload
```
After this, the timer will not run or start on boot.

### After updating source code
The service runs `python3` from your **project directory**, so it always uses the files on disk. After you pull or edit code:

- **If you only changed Python code or `.env`:**  
  No need to reinstall. The next run (on the hour) will already use the updated code. To run once immediately with the new code:
  ```bash
  sudo systemctl start holy-poly-dispute-research.service
  ```

- **If you changed the systemd unit files** (`systemd/holy-poly-dispute-research.service` or `.timer`), or you want to be sure the timer is using the latest units:  
  Re-run the install script. It will overwrite the units in `/etc/systemd/system/`, reload systemd, and restart the timer:
  ```bash
  ./install-systemd-timer.sh
  ```

## Message format
Header + list of markets. New markets are prefixed with 🟢.

Example:
```
Active Polymarket Events in Dispute

Filters:
- UMA disputes = 1

🟢 https://polymarket.com/market/... 
vol: v24h:$12.3k, vW:$120k, vM:$420k
tags: politics, us
```

## Notes / Edge cases
- If Telegram send fails, **state is not updated**, ensuring re‑try next run.
- If there are **no new markets**, nothing is sent.
- Tags are sourced from the parent event (`/events?slug=...`) and cached per run.
- Uses atomic state writes (tmp + rename).
