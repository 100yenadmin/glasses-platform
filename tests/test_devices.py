import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from glasses_plugin.devices import Connection, Identity, Outbox, Registry


def connection(user='alice', device='ray', version=1, camera=True):
    ws = SimpleNamespace(closed=False, send_json=AsyncMock(), close=AsyncMock())
    return Connection(ws, Identity(user, device), version, {'camera': camera})


@pytest.mark.asyncio
async def test_registry_replacement_and_camera():
    registry = Registry()
    old = connection()
    new = connection()
    await registry.add(old)
    await registry.add(new)
    old.ws.close.assert_awaited_once_with(code=4000, message=b'replaced')
    registry.remove(old)
    assert registry.live('alice') == [new]
    hud = connection(device='g2', camera=False)
    await registry.add(hud)
    assert registry.pick_camera('alice', hud) is new
    newer = connection(device='phone')
    await registry.add(newer)
    assert registry.pick_camera('alice') is newer
    assert registry.pick_camera('alice', new) is new
    newer.protocol = 0
    newer.capabilities = {}
    assert registry.pick_camera('alice') is newer
    assert registry.pick_camera('bob') is None
    registry.remove(new)
    assert new not in registry.live('alice')


@pytest.mark.asyncio
async def test_closed_send():
    conn = connection()
    conn.ws.closed = True
    assert not await conn.send_json({})
    conn.ws.closed = False
    conn.ws.send_json.side_effect = ConnectionResetError()
    assert not await conn.send_json({})


def test_outbox_order_ttl_capacity():
    now = [0]
    box = Outbox(2, 1, clock=lambda: now[0])
    for text in ('first', 'second', 'third'):
        box.append('alice', text, 'cron')
    box.append('bob', 'separate')
    assert [x[1] for x in box.drain('alice')] == ['second', 'third']
    assert box.drain('alice') == []
    now[0] = 3601
    assert box.drain('bob') == []
    box.append('alice', 'fresh')
    now[0] += 3601
    box.append('alice', 'new')
    assert [x[1] for x in box.drain('alice')] == ['new']
