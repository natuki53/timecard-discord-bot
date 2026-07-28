import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import discord


os.environ.setdefault('DB_DIR', '/tmp/timecard-test-db')
os.environ.setdefault('DISCORD_TOKEN', 'test-token')

MODULE_PATH = Path(__file__).resolve().parents[1] / 'timecard-main.py'
SPEC = importlib.util.spec_from_file_location('timecard_main', MODULE_PATH)
timecard_main = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(timecard_main)


def unknown_interaction_error():
    response = SimpleNamespace(
        status=404,
        reason='Not Found',
        headers={},
    )
    return discord.NotFound(
        response,
        {'code': 10062, 'message': 'Unknown interaction'},
    )


class InteractionHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_acknowledge_defers_immediately(self):
        response = SimpleNamespace(
            is_done=Mock(return_value=False),
            defer=AsyncMock(),
        )
        interaction = SimpleNamespace(id=1, response=response)

        acknowledged = await timecard_main.acknowledge_interaction(interaction)

        self.assertTrue(acknowledged)
        response.defer.assert_awaited_once_with(thinking=True)

    async def test_acknowledge_skips_an_existing_response(self):
        response = SimpleNamespace(
            is_done=Mock(return_value=True),
            defer=AsyncMock(),
        )
        interaction = SimpleNamespace(id=2, response=response)

        acknowledged = await timecard_main.acknowledge_interaction(interaction)

        self.assertTrue(acknowledged)
        response.defer.assert_not_awaited()

    async def test_expired_interaction_does_not_raise_while_acknowledging(self):
        response = SimpleNamespace(
            is_done=Mock(return_value=False),
            defer=AsyncMock(side_effect=unknown_interaction_error()),
        )
        interaction = SimpleNamespace(id=5, response=response)

        acknowledged = await timecard_main.acknowledge_interaction(interaction)

        self.assertFalse(acknowledged)

    async def test_message_edits_a_deferred_response(self):
        response = SimpleNamespace(
            is_done=Mock(return_value=True),
            send_message=AsyncMock(),
        )
        interaction = SimpleNamespace(
            id=3,
            response=response,
            edit_original_response=AsyncMock(),
        )

        delivered = await timecard_main.send_interaction_message(
            interaction,
            '完了',
        )

        self.assertTrue(delivered)
        interaction.edit_original_response.assert_awaited_once_with(content='完了')
        response.send_message.assert_not_awaited()

    async def test_message_uses_initial_response_when_not_deferred(self):
        response = SimpleNamespace(
            is_done=Mock(return_value=False),
            send_message=AsyncMock(),
        )
        interaction = SimpleNamespace(
            id=4,
            response=response,
            edit_original_response=AsyncMock(),
        )

        delivered = await timecard_main.send_interaction_message(
            interaction,
            '直接応答',
        )

        self.assertTrue(delivered)
        response.send_message.assert_awaited_once_with('直接応答')
        interaction.edit_original_response.assert_not_awaited()

    async def test_expired_interaction_does_not_raise_while_responding(self):
        response = SimpleNamespace(
            is_done=Mock(return_value=True),
            send_message=AsyncMock(),
        )
        interaction = SimpleNamespace(
            id=6,
            response=response,
            edit_original_response=AsyncMock(
                side_effect=unknown_interaction_error(),
            ),
        )

        delivered = await timecard_main.send_interaction_message(
            interaction,
            '期限切れ',
        )

        self.assertFalse(delivered)


if __name__ == '__main__':
    unittest.main()
