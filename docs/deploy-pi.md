# Deploying udhaari-bot to a Raspberry Pi

End-to-end guide for moving the bot from a dev laptop onto a Pi to host prod
traffic. Tested on a **Pi Zero 2 W (512MB RAM, aarch64, Pi OS Lite Trixie)**
but the steps are the same for Pi 4 / Pi 5 — those just have way more
headroom and you can skip the memory-tuning section.

The end state: a permanent `prod` instance of the bot, auto-starting on
boot via systemd, with traces in Langfuse tagged `environment=prod` so they
filter out from your dev work.

---

## 0. Prereqs

On the **Pi**:
- Pi OS Lite **64-bit** (Trixie or newer). 32-bit won't work — many of our
  Python deps don't ship 32-bit aarch wheels and you'd be compiling from
  source for hours.
- SSH access, on WiFi (Pi Zero 2 W has no Ethernet).
- ~5 GB free on the SD card (uv venv + Python deps weigh ~1.5 GB).

On your **dev machine**:
- A working `.env` file with all secrets populated (the same file you've
  been running with locally).
- Your ngrok authtoken (from <https://dashboard.ngrok.com/get-started/your-authtoken>).
- The IP / hostname of the Pi (`arp -a | grep -iE "raspberr|b8:27|dc:a6|d8:3a|e4:5f|2c:cf:67"`
  from a Mac will show it).

---

## 1. OS-level packages

SSH to the Pi, then:

```bash
sudo apt update
sudo apt install -y git tmux curl
```

(`tmux` is optional but very useful for running multi-pane sessions over
SSH while you iterate.)

---

## 2. Install `uv`

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
uv --version
```

The system Python (3.13 on Trixie) is fine — we don't need uv to manage
a separate Python install.

---

## 3. Clone the repo

```bash
cd ~
git clone https://github.com/prem-pa/auto-split.git
cd auto-split
git log --oneline -3
```

You should see the latest commit on `main` at the top.

---

## 4. Install Python deps

```bash
uv sync
```

> ⏱ This takes 5-15 minutes on a Pi Zero 2 W. CPU and SD I/O are the
> bottleneck, not network. Don't worry if `Compiling ...` lines appear for
> packages without aarch64 wheels (`tiktoken`, occasionally `cryptography`).

---

## 5. Copy your `.env` over

The cleanest path: scp the entire `.env` from your dev machine. Avoids
retyping (and mis-typing) secrets, and **keeps `TOKEN_ENCRYPTION_KEY`
identical** — critical so any previously-encrypted Splitwise tokens in
Supabase remain decryptable.

From your **dev machine**:

```bash
scp /path/to/auto-split/.env <username>@<pi-host>:~/auto-split/.env
```

Back on the **Pi**, flip the environment label:

```bash
cd ~/auto-split
grep -q '^ENVIRONMENT=' .env \
  && sed -i 's/^ENVIRONMENT=.*/ENVIRONMENT=prod/' .env \
  || echo 'ENVIRONMENT=prod' >> .env
grep '^ENVIRONMENT=' .env   # should print: ENVIRONMENT=prod
```

---

## 6. Smoke-test imports

```bash
uv run python -c "from app.main import app; print('routes:', sorted(r.path for r in app.routes))"
```

Expected (in any order):

```
INFO  app.main :: starting auto-split (base_url=https://<your>.ngrok-free.dev)
routes: ['/docs', '/docs/oauth2-redirect', '/health', '/oauth/callback', '/openapi.json', '/redoc', '/telegram/webhook']
```

First boot is slow (~10-20s) on Pi Zero — Python startup + 50+ imports
off slow SD. That cost is paid once per uvicorn restart; not per request.

Quick env-load check:

```bash
uv run python -c "
import app.observability as obs
from app.config import settings
print('LANGFUSE enabled?', obs.is_enabled())
print('environment      ', settings.environment)
print('PUBLIC_BASE_URL  ', settings.public_base_url)
"
```

`environment` should be `prod`, both other fields should be populated.

---

## 7. Install ngrok

```bash
curl -Lo /tmp/ngrok.tgz https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-arm64.tgz
sudo tar xzf /tmp/ngrok.tgz -C /usr/local/bin
rm /tmp/ngrok.tgz
ngrok version
```

Then authenticate with the same authtoken you use on your dev machine.
On your **dev machine**, print it once:

```bash
# macOS:
cat "$HOME/Library/Application Support/ngrok/ngrok.yml"
# Linux:
cat "$HOME/.config/ngrok/ngrok.yml"
```

Look for the `authtoken:` line and copy that value. Then on the **Pi**:

```bash
ngrok config add-authtoken <PASTE>
ngrok config check
```

Should print `Valid configuration file at /home/<user>/.config/ngrok/ngrok.yml`.

---

## 8. Stop the dev-machine services

Before the Pi takes over the tunnel, kill `uvicorn` and `ngrok` on your
dev machine. ngrok's free tier only allows **one tunnel client per account
at a time**, so if you don't kill the dev-side one first, the Pi will
preempt it and the dev side errors out anyway — but it's cleaner to do
it deliberately.

```bash
# In whichever terminals on your dev machine they're running:
Ctrl+C   # to ngrok
Ctrl+C   # to uvicorn
```

---

## 9. Install systemd services

The helper script writes both unit files, enables them for auto-start on
boot, and starts them immediately. Idempotent — safe to re-run later
when you `git pull` an update.

```bash
cd ~/auto-split
sudo bash scripts/install-pi-systemd.sh
```

It will print what it's doing and finish with the status of both services.
You're looking for `Active: active (running)` on both.

---

## 10. Verify

```bash
sudo systemctl status udhaari-bot.service --no-pager
sudo systemctl status udhaari-tunnel.service --no-pager
```

Tail live logs:

```bash
sudo journalctl -u udhaari-bot -u udhaari-tunnel -f
```

Send a test DM to the bot in Telegram. In the logs you should see:

```
... INFO  app.main :: starting auto-split (base_url=https://...)
... uvicorn :: Application startup complete.
... ngrok: started tunnel ...
... POST /telegram/webhook 200 OK
```

In Langfuse, the new trace should have `tags=[prod, dm, text]` (or
whatever combo matches your test message).

`Ctrl+C` to stop tailing — that doesn't stop the services.

---

## 11. (Optional but recommended) Reboot test

Confirm both services come back automatically after a power cycle:

```bash
sudo reboot
```

Wait ~30 seconds, SSH back in, and run:

```bash
sudo systemctl status udhaari-bot.service udhaari-tunnel.service --no-pager
```

If both are `active (running)` and the bot responds in Telegram, you're
done. Cable doesn't matter anymore; the Pi handles its own resurrection.

---

## Updating the bot later

```bash
cd ~/auto-split
git pull
uv sync                              # if pyproject.toml changed
sudo systemctl restart udhaari-bot.service
# tunnel doesn't need restart unless the unit file itself changed
```

If you change `.env` (e.g., new key, new tunnel domain):

```bash
sudo systemctl restart udhaari-bot.service udhaari-tunnel.service
```

If `scripts/install-pi-systemd.sh` itself changed (unit-file template
updated), re-run it:

```bash
sudo bash scripts/install-pi-systemd.sh
```

The script is idempotent — it overwrites and restarts.

---

## Memory tuning (Pi Zero 2 W specific)

The unit file sets `MemoryHigh=350M` and `MemoryMax=450M`. The bot's
resident set is right around 300-400MB depending on what it just did
(Gemini call inflates it briefly). Pi Zero 2 W has 512MB total; the OS
takes ~80MB; you have ~430MB to play with.

Watch for memory pressure:

```bash
# Check current resident:
systemctl status udhaari-bot.service | grep Memory

# Check journal for OOM kills:
sudo journalctl -u udhaari-bot.service | grep -i 'memory\|killed\|oom'
```

If you see repeated kills, options in order of effort:

1. **Raise the caps** — edit `scripts/install-pi-systemd.sh`, change
   `MemoryHigh` and `MemoryMax`, re-run the script.
2. **Add more swap** — Pi OS Trixie defaults to 415M swap (already
   plenty), but you can extend with `sudo dphys-swapfile setup` after
   editing `/etc/dphys-swapfile`.
3. **Upgrade to a Pi 4** — 2GB or 4GB. Same software, no more tuning.

---

## Troubleshooting

### Service won't start

```bash
sudo journalctl -u udhaari-bot.service -n 50 --no-pager
```

The last ~50 lines usually show the import error or config issue. Common
ones:

- `ModuleNotFoundError: No module named 'app'` → wrong `WorkingDirectory`.
  Re-run the install script.
- `pydantic_core...ValidationError` → `.env` is missing a required field
  or has a malformed value. Compare with `.env.example`.
- `Address already in use` → another uvicorn is running. `ss -ltnp | grep
  :8000` to find it, kill with `sudo kill <pid>`.

### Tunnel won't start

```bash
sudo journalctl -u udhaari-tunnel.service -n 30 --no-pager
```

Common: `tunnel session failed: ERR_NGROK_108` — your ngrok account
already has a session running elsewhere (probably your dev machine).
Kill the other one.

### Bot is up but Telegram messages don't arrive

- `curl -i http://127.0.0.1:8000/health` → should return `{"status":"ok"}`.
- `curl -i https://<your>.ngrok-free.dev/health` → should also return the
  same. If this fails, the tunnel isn't routing correctly.
- Check the Telegram webhook is pointed at your ngrok URL:
  `uv run python -c "import requests; from app.config import settings;
  print(requests.get(f'https://api.telegram.org/bot{settings.telegram_bot_token.get_secret_value()}/getWebhookInfo').json())"`.
  Expect `"url": "https://<your>.ngrok-free.dev/telegram/webhook"`.

### Want to go back to dev for a minute

```bash
sudo systemctl stop udhaari-bot.service udhaari-tunnel.service
# now uvicorn + ngrok on your dev machine can run normally
```

When you're done iterating:

```bash
sudo systemctl start udhaari-bot.service udhaari-tunnel.service
```

The `enable` from earlier persists across reboots — you don't lose
auto-start by stop/start cycles.
