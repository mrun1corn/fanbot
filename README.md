# FanBot (R720xd-hardened)

A **policy-driven Telegram bot and boot-time agent** for controlling **Dell PowerEdge R720 / R720xd** fan speeds via iDRAC using `ipmitool`.

FanBot is built for **hostile power environments** where **both the server and the Raspberry Pi reboot together**. It does not rely on RAM, uptime, or "last known state". Every boot converges back to the desired fan policy automatically.

## Key Design Goals

- Survive **power outages**
- Survive **server + Raspberry Pi rebooting together**
- Handle **iDRAC7 slow startup and amnesia**
- Re-assert fan control **until it sticks**
- Persist *intent*, not transient state
- Be quiet, predictable, and boring

## What This Bot Does

- Stores a **fan control policy on disk**
- On every boot:
  1. Waits for iDRAC to become reachable
  2. Waits for SDRs to be ready (R720xd-specific)
  3. Applies the saved fan policy
  4. Verifies and re-applies if iDRAC forgets
- Provides Telegram commands to change the **persistent policy**
- Automatically restores the policy after:
  - Server reboot
  - iDRAC reset
  - Raspberry Pi reboot
  - Power outage

## Supported Hardware

- **Dell PowerEdge R720 / R720xd**
- iDRAC7
- Raspberry Pi (or any Linux host running `ipmitool`)

Other Dell generations may work, but this bot explicitly accounts for R720xd quirks.

## Prerequisites

- Python **3.10+**
- `ipmitool` installed on the host running the bot
- Network access to iDRAC
- Telegram bot token
- (Recommended) UPS, but not required

## Installation

### 1. Clone and Prepare

```bash
git clone <your-repo>
cd fanbot
python -m venv venv
source venv/bin/activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

## Configuration

Copy the example environment file:

```bash
cp .env.example .env
```

Edit `.env`:

```
TELEGRAM_BOT_TOKEN=123456:ABCDEF
IDRAC_HOST=192.168.5.3
IDRAC_USERNAME=root
IDRAC_PASSWORD=calvin

# Optional: restrict who can control the bot
AUTHORIZED_CHAT_IDS=123456789,987654321
```

**Important:** Fan policy is not configured in `.env`. Policy is set via Telegram commands and persisted to disk automatically.

## Fan Policy Model

FanBot operates on a single persistent policy:

- **auto** – iDRAC controls fans
- **manual** – force fans to a fixed percentage

The policy:

- Is saved to disk
- Is enforced on every boot
- Survives power loss

## Running the Bot Manually

```bash
python fanbot.py
```

Startup behavior:

- Wait for iDRAC connectivity
- Wait for SDR readiness
- Apply saved policy
- Enter verification loop

## Running on Boot (systemd – Recommended)

Example `fanbot.service`:

```ini
[Unit]
Description=FanBot (Dell R720xd Fan Control)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=robin
WorkingDirectory=/home/robin/fanbot
EnvironmentFile=/home/robin/fanbot/.env
ExecStart=/home/robin/fanbot/venv/bin/python fanbot.py
Restart=always
RestartSec=10

# Allow iDRAC to wake up after power restore
ExecStartPre=/bin/sleep 45

[Install]
WantedBy=multi-user.target
```

Enable it:

```bash
sudo cp fanbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fanbot.service
```

## Logs

Check status:

```bash
systemctl status fanbot.service
```

View logs:

```bash
journalctl -u fanbot.service -f
```

## Telegram Commands

### `/status`

Show current policy and boot state.

Example response:

```
Policy: manual
Percent: 30
Boot complete: true
```

### `/manual <0-100>`

Set and persist a manual fan policy.

- Saved to disk immediately
- Applied now if iDRAC is ready
- Re-applied automatically after reboots

Example:

```
/manual 30
```

### `/auto`

Return control to iDRAC automatic fan management.

- Policy is saved
- Manual override disabled
- Survives reboots

## Reboot and Outage Behavior

FanBot assumes nothing survives. Every startup:

- Discovers current reality
- Enforces the saved policy

If iDRAC silently resets fan mode:

- Verification loop detects it
- Manual control is re-asserted

If power drops:

- Server and Pi reboot
- Policy is restored once iDRAC is ready
- No human intervention required

## R720xd Quirks Handled

- iDRAC reachable before SDRs are usable
- Manual fan mode silently reverting
- Double-assertion required for manual speed
- Temporary max-RPM fan spikes during boot
- Complete loss of settings after power loss

All explicitly accounted for.

## Files Created by the Bot

`fan_policy.json` – Stores the persistent fan policy.

Example:

```json
{
  "mode": "manual",
  "percent": 30
}
```

This file is the single source of truth.

## What This Bot Is (and Is Not)

### It Is

- A boot-time reconciliation agent
- A persistent fan policy enforcer
- Designed for unreliable firmware

### It Is Not

- A simple polling script
- Dependent on uptime
- Dependent on Telegram interaction to recover state

