"""A v1 `interrupt` frame is the wearer's stop button: Hermes gets its own /stop."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from glasses_plugin.tests.test_routing_fixes import opened


@pytest.mark.asyncio
@pytest.mark.parametrize('version,expected', [(1, ['/stop']), (0, [])])
async def test_interrupt_becomes_stop_for_v1_only(server, version, expected):
    adapter, client = server
    adapter.handle_message = AsyncMock()
    ws = await opened(client, version=version)
    await ws.send_json({'type': 'interrupt', 'id': 'i1'})
    await asyncio.sleep(0.1)
    events = [call.args[0] for call in adapter.handle_message.await_args_list]
    assert [e.text for e in events] == expected
    if expected:
        assert events[0].message_id == 'i1'
    await ws.close()
