"""A push is durable until any device delivers it, including explicit v1 acknowledgements."""
import asyncio

import pytest
from glasses_plugin.devices import Outbox
from glasses_plugin.tests.test_voice import device
from glasses_plugin.tests.test_routing_fixes import opened, until


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['push', 'outbox', 'pending', 'held'])
async def test_ack_pushes_keep_file_until_delivered(make_adapter, path):
    adapter = make_adapter()
    conn = await device(adapter, ack=True)
    if path == 'push':
        await adapter._push('alice', 'Remember', 'message', 'm1')
    elif path == 'outbox':
        adapter.outbox.append('alice', 'Remember', 'message', 'm1')
        conn.flushed = False
        await adapter._flush_outbox(conn)
    elif path == 'pending':
        conn.flushed = False
        await adapter._push('alice', 'Remember', 'message', 'm1')
        await adapter._flush_outbox(conn)
    else:
        conn.held = [(1e12, 'Remember', 'message', 'm1')]
        await adapter._release_held(conn)
    assert conn.ws.frames[0]['type'] == 'push'
    assert list(conn.unacked) == ['m1']
    assert [i[3] for i in Outbox(path=adapter.outbox.path).drain('alice')] == ['m1']
    adapter._mark_delivered('alice', 'm1')
    assert not conn.unacked
    assert Outbox(path=adapter.outbox.path).drain('alice') == []


@pytest.mark.asyncio
async def test_disconnect_without_ack_requeues(make_adapter):
    adapter = make_adapter()
    conn = await device(adapter, ack=True)
    await adapter._push('alice', 'Remember', 'message', 'm1')
    adapter._release(conn)
    assert not conn.unacked
    assert [i[3] for i in adapter.outbox.drain('alice')] == ['m1']


@pytest.mark.asyncio
@pytest.mark.parametrize('first_ack', [False, True])
async def test_other_device_delivery_forgets_unacked_in_either_order(make_adapter, first_ack):
    adapter = make_adapter()
    first = await device(adapter, ack=first_ack)
    second = await device(adapter, 'phone', ack=not first_ack)
    await adapter._push('alice', 'Remember', 'message', 'm1')
    assert not first.unacked and not second.unacked
    adapter._release(first)
    adapter._release(second)
    assert adapter.outbox.drain('alice') == []
    assert Outbox(path=adapter.outbox.path).drain('alice') == []


@pytest.mark.asyncio
async def test_one_device_ack_settles_all_devices_and_disconnects(make_adapter):
    adapter = make_adapter()
    a = await device(adapter, ack=True)
    b = await device(adapter, 'phone', ack=True)
    await adapter._push('alice', 'Remember', 'message', 'm1')
    adapter._mark_delivered('alice', 'm1')
    assert not a.unacked and not b.unacked
    adapter._release(a)
    adapter._release(b)
    assert adapter.outbox.drain('alice') == []


@pytest.mark.asyncio
async def test_legacy_ignores_ack_capability(make_adapter):
    adapter = make_adapter()
    conn = await device(adapter, version=0, ack=True)
    await adapter._push('alice', 'Remember', 'message', 'm1')
    assert not conn.unacked
    assert Outbox(path=adapter.outbox.path).drain('alice') == []


@pytest.mark.asyncio
async def test_socket_ack_known_id_only_and_capability_gate(server):
    adapter, client = server
    ws = await opened(client)
    await ws.send_json({'type': 'hello', 'device': {'capabilities': {'ack': True}}})
    await until(ws, 'hello_ack')
    await adapter._push('alice', 'Remember', 'message', 'm1')
    await until(ws, 'push')
    conn = adapter.registry.live('alice')[0]
    for id in ['unknown', ['m1']]:
        await ws.send_json({'type': 'ack', 'id': id})
        await ws.send_json({'type': 'ping'})
        await until(ws, 'pong')
        assert 'm1' in conn.unacked
    conn.capabilities['ack'] = False
    await ws.send_json({'type': 'ack', 'id': 'm1'})
    await ws.send_json({'type': 'ping'})
    await until(ws, 'pong')
    assert 'm1' in conn.unacked
    conn.capabilities['ack'] = True
    await ws.send_json({'type': 'ack', 'id': 'm1'})
    await ws.send_json({'type': 'ping'})
    await until(ws, 'pong')
    assert not conn.unacked
    assert Outbox(path=adapter.outbox.path).drain('alice') == []
    await ws.close()
