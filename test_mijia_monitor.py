from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from home_assistant_bluetooth import BluetoothServiceInfo

from mijia_monitor import (
    MIBEACON_UUID,
    MijiaCollector,
    WebhookConfig,
    load_config,
    mibeacon_product_id,
    normalize_mac,
    parse_bindkey,
    send_webhook,
)

ADDRESS = "A4:C1:38:12:34:56"
BINDKEY = bytes.fromhex("00112233445566778899aabbccddeeff")
OBJECTS = (
    b"\x04\x10\x02\xea\x00"  # 23.4 C
    b"\x06\x10\x02\x37\x02"  # 56.7 %
    b"\x0a\x10\x01\x58"  # 88 % battery
)


def service_info(payload: bytes, address: str = ADDRESS) -> BluetoothServiceInfo:
    return BluetoothServiceInfo(
        name="Mijia",
        address=address,
        rssi=-55,
        manufacturer_data={},
        service_data={MIBEACON_UUID: payload},
        service_uuids=[MIBEACON_UUID],
        source="hci0",
    )


def unencrypted_payload(product_id: int = 0x2832) -> bytes:
    mac = bytes.fromhex(ADDRESS.replace(":", ""))
    return (
        (0x5050).to_bytes(2, "little")
        + product_id.to_bytes(2, "little")
        + b"\x01"
        + mac[::-1]
        + OBJECTS
    )


def encrypted_payload() -> bytes:
    mac = bytes.fromhex(ADDRESS.replace(":", ""))
    counter = 1
    header = (
        (0x5958).to_bytes(2, "little")
        + (0x2832).to_bytes(2, "little")
        + bytes([counter])
        + mac[::-1]
    )
    extended_counter = bytes([counter, 0, 0])
    nonce = mac[::-1] + header[2:5] + extended_counter
    encrypted = AESCCM(BINDKEY, tag_length=4).encrypt(nonce, OBJECTS, b"\x11")
    return header + encrypted[:-4] + extended_counter + encrypted[-4:]


class _FakeResponse:
    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def read(self) -> bytes:
        return b"ok"

    def raise_for_status(self) -> None:
        return None


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def post(self, url: str, **kwargs: object) -> _FakeResponse:
        self.calls.append((url, kwargs))
        return _FakeResponse()


class HelpersTest(unittest.TestCase):
    def test_normalize_mac(self) -> None:
        self.assertEqual(normalize_mac("a4-c1-38-12-34-56"), ADDRESS)
        with self.assertRaises(ValueError):
            normalize_mac("not-a-mac")

    def test_parse_bindkey(self) -> None:
        self.assertEqual(parse_bindkey(BINDKEY.hex()), BINDKEY)
        self.assertIsNone(parse_bindkey(None))
        with self.assertRaises(ValueError):
            parse_bindkey("1234")

    def test_extract_product_id(self) -> None:
        self.assertEqual(mibeacon_product_id(unencrypted_payload()), 0x2832)
        self.assertIsNone(mibeacon_product_id(b"\x01\x02\x03"))

    def test_load_config_with_inline_bindkey(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "mac": ADDRESS.lower(),
                        "bindkey": BINDKEY.hex(),
                        "timeout": 600,
                        "json": True,
                        "webhook": {
                            "url": "https://example.com/hook",
                            "timeout": 5,
                            "headers": {"Authorization": "Bearer test"},
                        },
                    }
                ),
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertEqual(config.mac, ADDRESS)
        self.assertEqual(config.bindkey, BINDKEY)
        self.assertEqual(config.timeout, 600)
        self.assertTrue(config.json_output)
        self.assertEqual(config.webhook.url, "https://example.com/hook")
        self.assertEqual(config.webhook.headers["Authorization"], "Bearer test")

    def test_load_config_rejects_invalid_webhook_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps({"webhook": {"url": "not-a-url"}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "HTTP"):
                load_config(config_path)


class CollectorTest(unittest.TestCase):
    def test_unencrypted_reading_and_deduplication(self) -> None:
        collector = MijiaCollector()
        advertisement = service_info(unencrypted_payload())
        reading = collector.process(advertisement)

        self.assertIsNotNone(reading)
        assert reading is not None
        self.assertEqual(reading.address, ADDRESS)
        self.assertEqual(reading.model, "MJWSD05MMC")
        self.assertEqual(reading.temperature, 23.4)
        self.assertEqual(reading.humidity, 56.7)
        self.assertEqual(reading.battery, 88.0)
        self.assertEqual(reading.rssi, -55.0)
        self.assertIsNone(collector.process(advertisement))

    def test_mjwsd06mmc_hardware_revisions(self) -> None:
        for product_id in (0x55B5, 0x5BEA):
            with self.subTest(product_id=product_id):
                collector = MijiaCollector()
                reading = collector.process(
                    service_info(unencrypted_payload(product_id=product_id))
                )

                self.assertIsNotNone(reading)
                assert reading is not None
                self.assertEqual(reading.model, "MJWSD06MMC")
                self.assertEqual(reading.product_id, f"0x{product_id:04X}")
                self.assertEqual(reading.temperature, 23.4)
                self.assertEqual(reading.humidity, 56.7)

    def test_encrypted_reading_with_bindkey(self) -> None:
        collector = MijiaCollector(bindkey=BINDKEY)
        reading = collector.process(service_info(encrypted_payload()))

        self.assertIsNotNone(reading)
        assert reading is not None
        self.assertEqual(reading.temperature, 23.4)
        self.assertEqual(reading.humidity, 56.7)

    def test_encrypted_reading_without_bindkey_reports_notice(self) -> None:
        notices: list[str] = []
        collector = MijiaCollector(on_notice=notices.append)

        self.assertIsNone(collector.process(service_info(encrypted_payload())))
        self.assertTrue(any("bindkey" in notice for notice in notices))

    def test_filters_other_models_and_mac_addresses(self) -> None:
        collector = MijiaCollector(target_mac="00:11:22:33:44:55")
        self.assertIsNone(collector.process(service_info(unencrypted_payload())))

        collector = MijiaCollector()
        self.assertIsNone(
            collector.process(service_info(unencrypted_payload(product_id=0x055B)))
        )


class WebhookTest(unittest.IsolatedAsyncioTestCase):
    async def test_posts_reading_as_json_with_headers(self) -> None:
        reading = MijiaCollector().process(service_info(unencrypted_payload()))
        assert reading is not None
        session = _FakeSession()
        webhook = WebhookConfig(
            url="https://example.com/hook",
            headers={"Authorization": "Bearer test"},
        )

        await send_webhook(session, webhook, reading)  # type: ignore[arg-type]

        self.assertEqual(len(session.calls), 1)
        url, kwargs = session.calls[0]
        self.assertEqual(url, "https://example.com/hook")
        self.assertEqual(kwargs["json"]["temperature"], 23.4)  # type: ignore[index]
        self.assertEqual(
            kwargs["headers"],
            {"Authorization": "Bearer test"},
        )


if __name__ == "__main__":
    unittest.main()
