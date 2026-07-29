import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from bot_status import BotStatusReporter


class BotStatusReporterTests(unittest.IsolatedAsyncioTestCase):
    async def test_publish_writes_fixed_schema_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "status.json"
            reporter = BotStatusReporter(
                bot_id="example",
                discord_connected=lambda: True,
                gateway_latency_ms=lambda: 12.6,
                dependencies=lambda: [{"id": "engine", "connected": True}],
                output_path=output_path,
            )
            reporter.process_started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

            await reporter.publish(datetime(2026, 1, 1, 0, 0, 10, tzinfo=timezone.utc))

            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 1)
            self.assertEqual(payload["botId"], "example")
            self.assertEqual(payload["gatewayLatencyMs"], 13)
            self.assertEqual(payload["dependencies"], [{"id": "engine", "connected": True}])
            self.assertEqual([path.name for path in Path(directory).iterdir()], ["status.json"])

    async def test_disconnect_and_reconnect_are_reflected(self):
        state = {"connected": False, "latency": 45.0}
        with tempfile.TemporaryDirectory() as directory:
            reporter = BotStatusReporter(
                bot_id="example",
                discord_connected=lambda: state["connected"],
                gateway_latency_ms=lambda: state["latency"],
                output_path=Path(directory) / "status.json",
            )

            disconnected = await reporter.build_payload()
            state["connected"] = True
            reconnected = await reporter.build_payload()

            self.assertFalse(disconnected["discordConnected"])
            self.assertIsNone(disconnected["gatewayLatencyMs"])
            self.assertTrue(reconnected["discordConnected"])
            self.assertEqual(reconnected["gatewayLatencyMs"], 45)

    async def test_missing_output_path_disables_writes(self):
        with patch.dict("os.environ", {}, clear=True):
            reporter = BotStatusReporter(
                bot_id="example",
                discord_connected=lambda: True,
                gateway_latency_ms=lambda: float("nan"),
                output_path=None,
            )
        self.assertFalse(reporter.enabled)
        self.assertIsNone(await reporter.publish())

    async def test_invalid_provider_values_are_safely_normalized(self):
        reporter = BotStatusReporter(
            bot_id="example",
            discord_connected=lambda: "yes",
            gateway_latency_ms=lambda: float("inf"),
            dependencies=lambda: [
                {"id": "engine", "connected": "yes"},
                {"id": "", "connected": True},
            ],
        )

        payload = await reporter.build_payload()

        self.assertFalse(payload["discordConnected"])
        self.assertIsNone(payload["gatewayLatencyMs"])
        self.assertEqual(payload["dependencies"], [])


if __name__ == "__main__":
    unittest.main()
