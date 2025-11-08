#!/usr/bin/env python3
"""Telegram bot that monitors and controls Dell iDRAC fan speeds via ipmitool."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Sequence, Set

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

FAN_LINE_RE = re.compile(r"^(?P<name>[^|]+)\|\s+(?P<value>[\d.]+)\s*(?P<unit>RPM|%)", re.IGNORECASE)


class IPMIError(RuntimeError):
    """Raised when an ipmitool command fails."""


@dataclass
class BotConfig:
    telegram_token: str
    idrac_host: str
    idrac_username: str
    idrac_password: str
    poll_interval: float = 30.0
    notify_delta: int = 200
    reapply_interval: float = 120.0
    default_manual_percent: Optional[int] = None
    authorized_chat_ids: Optional[Set[int]] = None

    @classmethod
    def from_env(cls) -> "BotConfig":
        load_dotenv()

        token = os.getenv("TELEGRAM_BOT_TOKEN")
        host = os.getenv("IDRAC_HOST")
        username = os.getenv("IDRAC_USERNAME")
        password = os.getenv("IDRAC_PASSWORD")
        if not all([token, host, username, password]):
            missing = [
                name
                for name, value in [
                    ("TELEGRAM_BOT_TOKEN", token),
                    ("IDRAC_HOST", host),
                    ("IDRAC_USERNAME", username),
                    ("IDRAC_PASSWORD", password),
                ]
                if not value
            ]
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

        poll_interval = float(os.getenv("FAN_POLL_INTERVAL", "30"))
        notify_delta = int(os.getenv("FAN_NOTIFY_DELTA", "200"))
        reapply_interval = float(os.getenv("FAN_REAPPLY_INTERVAL", "120"))
        default_percent_raw = os.getenv("DEFAULT_FAN_PERCENT")
        default_percent: Optional[int] = None
        if default_percent_raw:
            try:
                value = int(default_percent_raw)
            except ValueError as exc:
                raise RuntimeError("DEFAULT_FAN_PERCENT must be an integer between 0 and 100.") from exc
            if not 0 <= value <= 100:
                raise RuntimeError("DEFAULT_FAN_PERCENT must be between 0 and 100.")
            default_percent = value

        authorized_raw = os.getenv("AUTHORIZED_CHAT_IDS")
        authorized = (
            {int(chat_id.strip()) for chat_id in authorized_raw.split(",") if chat_id.strip()}
            if authorized_raw
            else None
        )

        return cls(
            telegram_token=token,
            idrac_host=host,
            idrac_username=username,
            idrac_password=password,
            poll_interval=poll_interval,
            notify_delta=notify_delta,
            reapply_interval=reapply_interval,
            default_manual_percent=default_percent,
            authorized_chat_ids=authorized,
        )


class IPMIClient:
    """Helper for running ipmitool commands."""

    def __init__(self, host: str, username: str, password: str, timeout: float = 10.0) -> None:
        base = [
            "ipmitool",
            "-I",
            "lanplus",
            "-H",
            host,
            "-U",
            username,
            "-P",
            password,
        ]
        self._base_cmd: Sequence[str] = tuple(base)
        self._timeout = timeout

    def _run(self, *args: str) -> str:
        cmd = [*self._base_cmd, *args]
        try:
            logging.debug("Running %s", shlex.join(cmd))
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=True,
            )
        except FileNotFoundError as exc:
            raise IPMIError("ipmitool executable not found. Install ipmitool and retry.") from exc
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip()
            raise IPMIError(stderr or f"ipmitool failed with exit code {exc.returncode}") from exc
        except subprocess.TimeoutExpired as exc:
            raise IPMIError("ipmitool command timed out") from exc
        return completed.stdout

    def raw(self, *hex_bytes: str) -> str:
        return self._run("raw", *hex_bytes)

    def enable_static_fan_control(self) -> None:
        self.raw("0x30", "0x30", "0x01", "0x00")

    def disable_static_fan_control(self) -> None:
        self.raw("0x30", "0x30", "0x01", "0x01")

    def set_manual_fan_speed(self, percent: int) -> None:
        value = max(0, min(100, percent))
        hex_value = f"0x{value:02X}"
        self.raw("0x30", "0x30", "0x02", "0xff", hex_value)

    def apply_manual_speed(self, percent: int) -> None:
        self.enable_static_fan_control()
        self.set_manual_fan_speed(percent)

    def get_fan_readings(self) -> Dict[str, float]:
        output = self._run("sdr", "list", "full")
        readings: Dict[str, float] = {}
        for line in output.splitlines():
            match = FAN_LINE_RE.match(line.strip())
            if not match:
                continue
            name = match.group("name").strip()
            value_str = match.group("value")
            try:
                value = float(value_str)
            except ValueError:
                continue
            # assume RPM is what we care about
            readings[name] = value
        if not readings:
            raise IPMIError("Unable to parse fan readings from ipmitool output")
        return readings


class FanBot:
    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.ipmi = IPMIClient(config.idrac_host, config.idrac_username, config.idrac_password)
        self.application = Application.builder().token(config.telegram_token).build()
        self.manual_speed: Optional[int] = None
        self.last_speed_push: Optional[float] = None
        self.failed_reapply: bool = False
        self.subscribers: Set[int] = set()
        self.last_readings: Dict[str, float] = {}

    def register_handlers(self) -> None:
        self.application.add_handler(CommandHandler(["start", "help"], self.cmd_start))
        self.application.add_handler(CommandHandler("status", self.cmd_status))
        self.application.add_handler(CommandHandler("set_speed", self.cmd_set_speed))
        self.application.add_handler(CommandHandler("setspeed", self.cmd_set_speed))
        self.application.add_handler(CommandHandler("auto", self.cmd_auto))

        self.application.job_queue.run_repeating(
            self.monitor_fans,
            interval=self.config.poll_interval,
            first=0,
        )
        self.application.job_queue.run_once(self._apply_default_speed, when=0)

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_chat:
            return
        if not self._is_authorized(update.effective_chat.id):
            await update.effective_chat.send_message("You are not authorized to control this bot.")
            return
        self.subscribers.add(update.effective_chat.id)
        await update.effective_chat.send_message(
            "Welcome! Use /status to read fan RPM and /set_speed <0-100> to set manual fan control. "
            "Send /auto to return to iDRAC automatic fan handling."
        )

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_chat or not self._is_authorized(update.effective_chat.id):
            return
        try:
            readings = await asyncio.to_thread(self.ipmi.get_fan_readings)
        except IPMIError as exc:
            await update.effective_chat.send_message(f"Failed to read fans: {exc}")
            return
        message = self._format_readings(readings)
        await update.effective_chat.send_message(message)

    async def cmd_set_speed(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_chat or not self._is_authorized(update.effective_chat.id):
            return
        if not context.args:
            await update.effective_chat.send_message("Usage: /set_speed <0-100>")
            return
        try:
            value = context.args[0].rstrip("%")
            percent = int(value)
        except ValueError:
            await update.effective_chat.send_message("Please provide an integer fan percent between 0 and 100.")
            return
        if not 0 <= percent <= 100:
            await update.effective_chat.send_message("Fan percent must be between 0 and 100.")
            return
        try:
            await asyncio.to_thread(self.ipmi.apply_manual_speed, percent)
        except IPMIError as exc:
            await update.effective_chat.send_message(f"Failed to set speed: {exc}")
            return
        self.manual_speed = percent
        self.last_speed_push = time.monotonic()
        self.failed_reapply = False
        self.subscribers.add(update.effective_chat.id)
        await update.effective_chat.send_message(
            f"Manual fan speed set to {percent}% (byte 0x{percent:02X})."
        )

    async def cmd_auto(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_chat or not self._is_authorized(update.effective_chat.id):
            return
        try:
            await asyncio.to_thread(self.ipmi.disable_static_fan_control)
        except IPMIError as exc:
            await update.effective_chat.send_message(f"Failed to return to auto control: {exc}")
            return
        self.manual_speed = None
        self.last_speed_push = None
        await update.effective_chat.send_message("Fan control returned to iDRAC automatic mode.")

    async def monitor_fans(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reapply_manual_speed_if_needed(context)
        try:
            readings = await asyncio.to_thread(self.ipmi.get_fan_readings)
        except IPMIError as exc:
            logging.warning("Fan poll failed: %s", exc)
            return
        if not self.last_readings:
            self.last_readings = readings
            return
        changed = self._diff_readings(self.last_readings, readings, self.config.notify_delta)
        self.last_readings = readings
        if changed:
            text = "Fan speed change detected:\n" + "\n".join(
                f"- {name}: {value:.0f} RPM" for name, value in changed.items()
            )
            await self._broadcast(context, text)

    async def _apply_default_speed(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.config.default_manual_percent is None:
            return
        percent = self.config.default_manual_percent
        try:
            await asyncio.to_thread(self.ipmi.apply_manual_speed, percent)
        except IPMIError as exc:
            logging.error("Failed to apply default fan speed %s%%: %s", percent, exc)
            return
        self.manual_speed = percent
        self.last_speed_push = time.monotonic()
        self.failed_reapply = False
        logging.info("Default manual fan speed %s%% applied.", percent)

    async def _reapply_manual_speed_if_needed(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.manual_speed is None:
            return
        now = time.monotonic()
        if self.last_speed_push and now - self.last_speed_push < self.config.reapply_interval:
            return
        try:
            await asyncio.to_thread(self.ipmi.apply_manual_speed, self.manual_speed)
        except IPMIError as exc:
            if not self.failed_reapply:
                logging.warning("Manual speed refresh failed: %s", exc)
            self.failed_reapply = True
            return
        self.last_speed_push = now
        if self.failed_reapply:
            await self._broadcast(
                context,
                f"Manual fan speed {self.manual_speed}% re-applied after iDRAC became reachable again.",
            )
        self.failed_reapply = False

    def _format_readings(self, readings: Dict[str, float]) -> str:
        lines = ["Fan readings:"]
        for name, value in readings.items():
            lines.append(f"- {name}: {value:.0f} RPM")
        if self.manual_speed is not None:
            lines.append(f"Manual override locked at {self.manual_speed}% (0x{self.manual_speed:02X}).")
        return "\n".join(lines)

    @staticmethod
    def _diff_readings(
        old: Dict[str, float], new: Dict[str, float], threshold: int
    ) -> Dict[str, float]:
        changed: Dict[str, float] = {}
        for name, value in new.items():
            previous = old.get(name)
            if previous is None:
                changed[name] = value
                continue
            if abs(value - previous) >= threshold:
                changed[name] = value
        return changed

    async def _broadcast(self, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        if not self.subscribers:
            return
        for chat_id in list(self.subscribers):
            try:
                await context.bot.send_message(chat_id=chat_id, text=text)
            except Exception as exc:  # pylint: disable=broad-except
                logging.warning("Failed to notify chat %s: %s", chat_id, exc)

    def _is_authorized(self, chat_id: int) -> bool:
        if self.config.authorized_chat_ids is None:
            return True
        return chat_id in self.config.authorized_chat_ids

    def run(self) -> None:
        self.register_handlers()
        logging.info(
            "Starting fan bot for %s (poll every %.0fs)",
            self.config.idrac_host,
            self.config.poll_interval,
        )
        self.application.run_polling(stop_signals=None)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    config = BotConfig.from_env()
    bot = FanBot(config)
    bot.run()


if __name__ == "__main__":
    main()
