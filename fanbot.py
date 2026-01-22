#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Dict, Optional, Set

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

STATE_FILE = "fan_policy.json"

# ---------------- Exceptions ----------------

class IPMIError(RuntimeError):
    pass


# ---------------- Configuration ----------------

@dataclass
class BotConfig:
    telegram_token: str
    idrac_host: str
    idrac_username: str
    idrac_password: str
    authorized_chat_ids: Optional[Set[int]]
    verify_interval: int = 30

    @classmethod
    def from_env(cls) -> "BotConfig":
        load_dotenv()

        token = os.getenv("TELEGRAM_BOT_TOKEN")
        host = os.getenv("IDRAC_HOST")
        user = os.getenv("IDRAC_USERNAME")
        pwd = os.getenv("IDRAC_PASSWORD")

        if not all([token, host, user, pwd]):
            raise RuntimeError("Missing required environment variables")

        auth_raw = os.getenv("AUTHORIZED_CHAT_IDS")
        auth = (
            {int(x.strip()) for x in auth_raw.split(",") if x.strip()}
            if auth_raw
            else None
        )

        return cls(
            telegram_token=token,
            idrac_host=host,
            idrac_username=user,
            idrac_password=pwd,
            authorized_chat_ids=auth,
        )


# ---------------- IPMI Client ----------------

class IPMIClient:
    def __init__(self, host: str, user: str, password: str, timeout: int = 10):
        self.base = [
            "ipmitool",
            "-I", "lanplus",
            "-H", host,
            "-U", user,
            "-P", password,
        ]
        self.timeout = timeout

    def _run(self, *args: str) -> str:
        cmd = [*self.base, *args]
        logging.debug("Running: %s", shlex.join(cmd))
        try:
            p = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=True,
            )
            return p.stdout.strip()
        except Exception as exc:
            raise IPMIError(str(exc)) from exc

    # ---- Readiness checks ----

    def reachable(self) -> bool:
        try:
            self._run("chassis", "power", "status")
            return True
        except IPMIError:
            return False

    def sdr_ready(self) -> bool:
        try:
            out = self._run("sdr", "elist")
            return "Fan" in out
        except IPMIError:
            return False

    # ---- Fan control ----

    def manual_mode_active(self) -> bool:
        try:
            out = self._run("raw", "0x30", "0x30", "0x01")
            return out.strip() == "00"
        except IPMIError:
            return False

    def enable_manual(self) -> None:
        self._run("raw", "0x30", "0x30", "0x01", "0x00")

    def disable_manual(self) -> None:
        self._run("raw", "0x30", "0x30", "0x01", "0x01")

    def set_fan_percent(self, percent: int) -> None:
        val = max(0, min(100, percent))
        self._run(
            "raw",
            "0x30", "0x30", "0x02", "0xff",
            f"0x{val:02x}"
        )

    def apply_manual_speed(self, percent: int) -> None:
        # R720xd requires double-assertion
        self.enable_manual()
        time.sleep(1)
        self.set_fan_percent(percent)
        time.sleep(1)
        self.set_fan_percent(percent)


# ---------------- Policy Handling ----------------

def load_policy() -> Dict:
    if not os.path.exists(STATE_FILE):
        return {"mode": "auto", "percent": None}
    with open(STATE_FILE) as f:
        return json.load(f)

def save_policy(policy: Dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(policy, f)


# ---------------- Fan Controller ----------------

class FanController:
    def __init__(self, ipmi: IPMIClient):
        self.ipmi = ipmi
        self.policy = load_policy()
        self.boot_completed = False

    async def boot_sequence(self):
        logging.info("Boot sequence started")

        while not await asyncio.to_thread(self.ipmi.reachable):
            logging.info("Waiting for iDRAC...")
            await asyncio.sleep(10)

        while not await asyncio.to_thread(self.ipmi.sdr_ready):
            logging.info("Waiting for SDRs...")
            await asyncio.sleep(10)

        await self.enforce_policy()
        self.boot_completed = True
        logging.info("Boot sequence complete")

    async def enforce_policy(self):
        mode = self.policy.get("mode")

        if mode == "auto":
            await asyncio.to_thread(self.ipmi.disable_manual)
            return

        if mode == "manual":
            percent = self.policy.get("percent")
            if percent is None:
                return
            await asyncio.to_thread(self.ipmi.apply_manual_speed, percent)

    async def verify_loop(self):
        while True:
            try:
                if self.policy["mode"] == "manual":
                    active = await asyncio.to_thread(self.ipmi.manual_mode_active)
                    if not active:
                        logging.warning("Manual mode lost, reapplying")
                        await self.enforce_policy()
            except Exception as exc:
                logging.warning("Verify failed: %s", exc)

            await asyncio.sleep(30)


# ---------------- Telegram Bot ----------------

class FanBot:
    def __init__(self, config: BotConfig, controller: FanController):
        self.config = config
        self.controller = controller
        self.app = Application.builder().token(config.telegram_token).build()

    def authorized(self, chat_id: int) -> bool:
        if self.config.authorized_chat_ids is None:
            return True
        return chat_id in self.config.authorized_chat_ids

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat or not self.authorized(update.effective_chat.id):
            return
        p = self.controller.policy
        await update.effective_chat.send_message(
            f"Policy: {p['mode']}\n"
            f"Percent: {p.get('percent')}\n"
            f"Boot complete: {self.controller.boot_completed}"
        )

    async def cmd_manual(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat or not self.authorized(update.effective_chat.id):
            return
        if not ctx.args:
            return
        percent = int(ctx.args[0])
        self.controller.policy = {"mode": "manual", "percent": percent}
        save_policy(self.controller.policy)
        await self.controller.enforce_policy()
        await update.effective_chat.send_message(
            f"Manual policy set: {percent}% (persistent)"
        )

    async def cmd_auto(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat or not self.authorized(update.effective_chat.id):
            return
        self.controller.policy = {"mode": "auto", "percent": None}
        save_policy(self.controller.policy)
        await self.controller.enforce_policy()
        await update.effective_chat.send_message("Auto fan policy enabled")

    def run(self):
        self.app.add_handler(CommandHandler("status", self.cmd_status))
        self.app.add_handler(CommandHandler("manual", self.cmd_manual))
        self.app.add_handler(CommandHandler("auto", self.cmd_auto))

        self.app.run_polling(stop_signals=None)


# ---------------- Main ----------------

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s"
    )

    config = BotConfig.from_env()
    ipmi = IPMIClient(
        config.idrac_host,
        config.idrac_username,
        config.idrac_password,
    )
    controller = FanController(ipmi)

    asyncio.create_task(controller.boot_sequence())
    asyncio.create_task(controller.verify_loop())

    bot = FanBot(config, controller)
    bot.run()


if __name__ == "__main__":
    asyncio.run(main())
