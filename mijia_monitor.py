#!/usr/bin/env python3
"""Read supported Xiaomi temperature/humidity monitors from BLE broadcasts."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import aiohttp
from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from home_assistant_bluetooth import BluetoothServiceInfo
from xiaomi_ble import EncryptionScheme, XiaomiBluetoothDeviceData

MIBEACON_UUID = "0000fe95-0000-1000-8000-00805f9b34fb"
TARGET_MODELS = {
    0x2832: "MJWSD05MMC",
    0x4C47: "MJWSD05MMC",
    0x55B5: "MJWSD06MMC",
    0x5BEA: "MJWSD06MMC",
}
TARGET_PRODUCT_IDS = frozenset(TARGET_MODELS)
DEFAULT_MODEL = "Xiaomi temperature/humidity monitor"
VALUE_KEYS = ("temperature", "humidity", "battery", "signal_strength")


@dataclass(frozen=True)
class Reading:
    """A normalized sensor reading."""

    timestamp: str
    address: str
    model: str
    product_id: str
    temperature: float | None
    humidity: float | None
    battery: float | None
    rssi: float | None


@dataclass(frozen=True)
class WebhookConfig:
    """Webhook delivery settings."""

    url: str | None = None
    timeout: float = 10.0
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AppConfig:
    """Validated application configuration."""

    mac: str | None = None
    bindkey: bytes | None = None
    adapter: str | None = None
    timeout: float = 0.0
    once: bool = False
    json_output: bool = False
    raw: bool = False
    verbose: bool = False
    webhook: WebhookConfig = field(default_factory=WebhookConfig)


@dataclass
class _DeviceState:
    parser: XiaomiBluetoothDeviceData
    product_id: int
    model: str
    last_measurement: tuple[float | None, float | None, float | None] | None = None
    encryption_notice_sent: bool = False


def normalize_mac(value: str) -> str:
    """Return a canonical Bluetooth MAC address."""
    normalized = value.strip().replace("-", ":").upper()
    if not re.fullmatch(r"[0-9A-F]{2}(?::[0-9A-F]{2}){5}", normalized):
        raise ValueError(f"invalid Bluetooth MAC address: {value}")
    return normalized


def parse_bindkey(value: str | None) -> bytes | None:
    """Parse a 16-byte MiBeacon bindkey."""
    if value is None or not value.strip():
        return None
    normalized = value.strip().replace(" ", "")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", normalized):
        raise ValueError("bindkey must contain exactly 32 hexadecimal characters")
    return bytes.fromhex(normalized)


def load_config(path: Path) -> AppConfig:
    """Load and validate a JSON configuration file."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"could not read config file: {error}") from error
    except json.JSONDecodeError as error:
        raise ValueError(
            f"invalid JSON in config file at line {error.lineno}, column {error.colno}"
        ) from error
    if not isinstance(raw, dict):
        raise ValueError("config root must be a JSON object")

    mac_value = _optional_string(raw, "mac")
    mac = normalize_mac(mac_value) if mac_value else None
    adapter = _optional_string(raw, "adapter")
    timeout = _number(raw, "timeout", 0.0)
    if timeout < 0:
        raise ValueError("config field 'timeout' cannot be negative")

    bindkey_value = _optional_string(raw, "bindkey")

    webhook_raw = raw.get("webhook", {})
    if webhook_raw is None:
        webhook_raw = {}
    if not isinstance(webhook_raw, dict):
        raise ValueError("config field 'webhook' must be an object")
    webhook_url = _optional_string(webhook_raw, "url")
    if webhook_url:
        parsed_url = urlsplit(webhook_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("config field 'webhook.url' must be an HTTP(S) URL")
    webhook_timeout = _number(webhook_raw, "timeout", 10.0)
    if webhook_timeout <= 0:
        raise ValueError("config field 'webhook.timeout' must be greater than zero")
    headers_raw = webhook_raw.get("headers", {})
    if not isinstance(headers_raw, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in headers_raw.items()
    ):
        raise ValueError("config field 'webhook.headers' must contain string values")

    return AppConfig(
        mac=mac,
        bindkey=parse_bindkey(bindkey_value),
        adapter=adapter,
        timeout=timeout,
        once=_boolean(raw, "once", False),
        json_output=_boolean(raw, "json", False),
        raw=_boolean(raw, "raw", False),
        verbose=_boolean(raw, "verbose", False),
        webhook=WebhookConfig(
            url=webhook_url,
            timeout=webhook_timeout,
            headers=dict(headers_raw),
        ),
    )


def _optional_string(data: dict[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"config field '{key}' must be a string or null")
    stripped = value.strip()
    return stripped or None


def _number(data: dict[str, object], key: str, default: float) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"config field '{key}' must be a number")
    return float(value)


def _boolean(data: dict[str, object], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"config field '{key}' must be a boolean")
    return value


def mibeacon_payload(service_data: dict[str, bytes]) -> bytes | None:
    """Extract the MiBeacon service payload from a BLE advertisement."""
    for uuid, payload in service_data.items():
        if uuid.lower() == MIBEACON_UUID:
            return payload
    return None


def mibeacon_product_id(payload: bytes) -> int | None:
    """Extract the little-endian MiBeacon product ID."""
    if len(payload) < 4:
        return None
    return int.from_bytes(payload[2:4], byteorder="little")


class MijiaCollector:
    """Track and decode supported Xiaomi monitor advertisements."""

    def __init__(
        self,
        *,
        bindkey: bytes | None = None,
        target_mac: str | None = None,
        on_notice: Callable[[str], None] | None = None,
    ) -> None:
        self._bindkey = bindkey
        self._target_mac = normalize_mac(target_mac) if target_mac else None
        self._on_notice = on_notice or (lambda _message: None)
        self._states: dict[str, _DeviceState] = {}

    def process(self, service_info: BluetoothServiceInfo) -> Reading | None:
        """Decode one BLE advertisement and return a changed measurement."""
        try:
            address = normalize_mac(service_info.address)
        except ValueError:
            return None
        if self._target_mac and address != self._target_mac:
            return None

        payload = mibeacon_payload(service_info.service_data)
        if payload is None:
            return None
        product_id = mibeacon_product_id(payload)
        if product_id not in TARGET_PRODUCT_IDS:
            return None

        state = self._states.get(address)
        if state is None:
            state = _DeviceState(
                parser=XiaomiBluetoothDeviceData(bindkey=self._bindkey),
                product_id=product_id,
                model=TARGET_MODELS[product_id],
            )
            self._states[address] = state
            self._on_notice(
                f"found {state.model} at {address}, product_id=0x{product_id:04X}"
            )

        update = state.parser.update(service_info)
        values = {
            str(key.key): value.native_value
            for key, value in update.entity_values.items()
            if str(key.key) in VALUE_KEYS
        }

        if (
            state.parser.encryption_scheme != EncryptionScheme.NONE
            and not state.parser.bindkey_verified
            and not state.encryption_notice_sent
        ):
            if self._bindkey is None:
                self._on_notice(
                    f"{address} uses encrypted MiBeacon data; provide --bindkey "
                    "or --bindkey-file"
                )
            else:
                self._on_notice(
                    f"{address} could not be decrypted; verify that the bindkey "
                    "belongs to this MAC address"
                )
            state.encryption_notice_sent = True

        temperature = _number_or_none(values.get("temperature"))
        humidity = _number_or_none(values.get("humidity"))
        battery = _number_or_none(values.get("battery"))
        rssi = _number_or_none(values.get("signal_strength"))
        if temperature is None and humidity is None:
            return None

        measurement = (temperature, humidity, battery)
        if measurement == state.last_measurement:
            return None
        state.last_measurement = measurement

        return Reading(
            timestamp=datetime.now().astimezone().isoformat(timespec="seconds"),
            address=address,
            model=state.model,
            product_id=f"0x{state.product_id:04X}",
            temperature=temperature,
            humidity=humidity,
            battery=battery,
            rssi=rssi,
        )


def _number_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def format_reading(reading: Reading, *, json_output: bool) -> str:
    """Format one reading for stdout."""
    if json_output:
        return json.dumps(asdict(reading), ensure_ascii=False, separators=(",", ":"))

    fields = [reading.timestamp, reading.model, reading.address]
    if reading.temperature is not None:
        fields.append(f"temperature={reading.temperature:g} C")
    if reading.humidity is not None:
        fields.append(f"humidity={reading.humidity:g} %")
    if reading.battery is not None:
        fields.append(f"battery={reading.battery:g} %")
    if reading.rssi is not None:
        fields.append(f"rssi={reading.rssi:g} dBm")
    return "  ".join(fields)


def build_service_info(
    device: BLEDevice, advertisement: AdvertisementData, source: str
) -> BluetoothServiceInfo:
    """Convert a Bleak advertisement to the parser's transport object."""
    return BluetoothServiceInfo(
        name=advertisement.local_name or device.name or DEFAULT_MODEL,
        address=device.address,
        rssi=advertisement.rssi,
        manufacturer_data=advertisement.manufacturer_data,
        service_data=advertisement.service_data,
        service_uuids=advertisement.service_uuids,
        source=source,
    )


async def send_webhook(
    session: aiohttp.ClientSession,
    webhook: WebhookConfig,
    reading: Reading,
) -> None:
    """POST one reading to the configured webhook."""
    if webhook.url is None:
        return
    async with session.post(
        webhook.url,
        json=asdict(reading),
        headers=webhook.headers,
    ) as response:
        await response.read()
        response.raise_for_status()


async def scan(config: AppConfig) -> int:
    """Scan until interrupted, timed out, or a complete reading is received."""
    stop_event = asyncio.Event()
    received_count = 0
    webhook_tasks: set[asyncio.Task[None]] = set()

    def notice(message: str) -> None:
        print(f"[mijia-monitor] {message}", file=sys.stderr, flush=True)

    collector = MijiaCollector(
        bindkey=config.bindkey,
        target_mac=config.mac,
        on_notice=notice,
    )
    webhook_session = (
        aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=config.webhook.timeout)
        )
        if config.webhook.url
        else None
    )

    def webhook_done(task: asyncio.Task[None]) -> None:
        webhook_tasks.discard(task)
        try:
            task.result()
        except Exception as error:
            notice(f"webhook failed: {error}")

    def on_advertisement(device: BLEDevice, advertisement: AdvertisementData) -> None:
        nonlocal received_count
        payload = mibeacon_payload(advertisement.service_data)
        if config.raw and payload is not None:
            product_id = mibeacon_product_id(payload)
            if product_id in TARGET_PRODUCT_IDS:
                notice(f"raw {device.address.upper()} {payload.hex()}")

        reading = collector.process(
            build_service_info(device, advertisement, config.adapter or "hci0")
        )
        if reading is None:
            return
        received_count += 1
        print(format_reading(reading, json_output=config.json_output), flush=True)
        if webhook_session is not None:
            task = asyncio.create_task(
                send_webhook(webhook_session, config.webhook, reading)
            )
            webhook_tasks.add(task)
            task.add_done_callback(webhook_done)
        if config.once and reading.temperature is not None and reading.humidity is not None:
            stop_event.set()

    scanner_options = {}
    if config.adapter:
        scanner_options["bluez"] = {"adapter": config.adapter}
    scanner = BleakScanner(detection_callback=on_advertisement, **scanner_options)

    notice(
        "scanning for MJWSD05MMC/MJWSD06MMC broadcasts"
        + (f" on {config.adapter}" if config.adapter else "")
        + (f" from {config.mac}" if config.mac else "")
    )
    scanner_started = False
    try:
        await scanner.start()
        scanner_started = True
        if config.timeout > 0:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=config.timeout)
            except TimeoutError:
                notice(f"scan timed out after {config.timeout:g} seconds")
        else:
            await stop_event.wait()
    finally:
        if scanner_started:
            await scanner.stop()
        if webhook_tasks:
            await asyncio.gather(*webhook_tasks, return_exceptions=True)
        if webhook_session is not None:
            await webhook_session.close()

    return 0 if received_count else 2


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read Xiaomi MJWSD05MMC/MJWSD06MMC temperature/humidity BLE broadcasts."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.json"),
        help="JSON configuration file (default: config.json)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ValueError as error:
        parser.error(str(error))

    logging.basicConfig(
        level=logging.INFO if config.verbose else logging.ERROR,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(scan(config))
    except KeyboardInterrupt:
        print("\n[mijia-monitor] stopped", file=sys.stderr)
        return 130
    except Exception as error:  # BlueZ/D-Bus errors need a concise operator message.
        print(f"[mijia-monitor] scan failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
