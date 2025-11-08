# FanBot

A Python Telegram bot (python-telegram-bot) that talks to iDRAC via `ipmitool` to watch fan RPMs, notify you when they change, and keep a manual fan speed applied even after the server or iDRAC reboots.

## Prerequisites
- Python 3.10+
- `ipmitool` installed on the host where the bot runs and reachable iDRAC credentials
- Telegram bot token (the one you provided) and optionally a list of authorized chat IDs

## Setup
1. (Optional) create a virtual environment.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill in:
   ```ini
   TELEGRAM_BOT_TOKEN=8524785566:AAExrH6QvdfK7JXu_dpLSYenx2UWDqh0Idk
   IDRAC_HOST=192.168.5.3
   IDRAC_USERNAME=root
   IDRAC_PASSWORD=calvin
   AUTHORIZED_CHAT_IDS=<comma separated chat ids>
   FAN_POLL_INTERVAL=30
   FAN_NOTIFY_DELTA=200
   FAN_REAPPLY_INTERVAL=120
   DEFAULT_FAN_PERCENT=30
   ```
   Adjust the poll interval (seconds), RPM delta required for a notification, and how often the bot should re-apply your chosen fan speed.
   `DEFAULT_FAN_PERCENT` tells the bot to lock fans to that percentage at startup and keep retrying after boot until iDRAC accepts the override.

## Running the bot
```bash
python fanbot.py
```
Leave the process running (e.g., systemd service or tmux) so it can keep polling the fans and push Telegram notifications.

## Auto-start on boot (systemd)
1. Edit `fanbot.service` so `User`, `Group`, `WorkingDirectory`, and `EnvironmentFile` match your Pi’s setup (defaults are `/home/robin/fanbot`).
2. Copy it into systemd and enable it:
   ```bash
   sudo cp fanbot.service /etc/systemd/system/fanbot.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now fanbot.service
   ```
3. Check status / logs if needed:
   ```bash
   systemctl status fanbot.service
   journalctl -u fanbot.service -f
   ```
Systemd will now start the bot at boot and restart it if it crashes, so fan control re-applies automatically after power cycles.

## Telegram commands
- `/start` or `/help` – authorize the chat (if restricted) and show usage.
- `/status` – fetch current fan RPM from `ipmitool sdr list full` and show whether manual control is active.
- `/set_speed <0-100>` – enables static control (`raw 0x30 0x30 0x01 0x00`) and sets the manual fan byte (`raw 0x30 0x30 0x02 0xff 0xNN`).
- `/auto` – disables static control so iDRAC manages fan curves again.

## How monitoring works
- The job queue polls iDRAC every `FAN_POLL_INTERVAL` seconds.
- When any fan changes by at least `FAN_NOTIFY_DELTA` RPM since the last reading, all subscribed chats are notified.
- If you set a manual speed, the bot stores the percentage and replays the enable/set sequence every `FAN_REAPPLY_INTERVAL` seconds or as soon as iDRAC becomes reachable after a reboot, ensuring the noise stays low even after restarts.
