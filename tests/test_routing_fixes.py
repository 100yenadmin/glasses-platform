"""Regression tests for review findings F1–F4 (routing of finals, pushes, queued pushes and approval prompts)."""
import asyncio

import pytest


async def until(ws, kind):
    async with asyncio.timeout(3):
        while True:
            frame = await ws.receive_json()
            if frame['type'] == kind:
                return frame


async def opened(client, token='tok-alice-1', version=1, camera=True):
    ws = await client.ws_connect('/voice', params={'token': token})
    assert (await until(ws, 'welcome'))['protocol_max'] == 1
    if version:
        await ws.send_json({'type': 'hello', 'protocol': 1, 'device': {'capabilities': {'camera': camera}}})
        assert (await until(ws, 'hello_ack'))['protocol'] == 1
    return ws


@pytest.mark.asyncio
async def test_v1_client_gets_queued_push_as_push_frame(server):
    adapter, client = server
    await adapter.send('alice', 'queued while offline', metadata={'job_id': 'j1'})
    ws = await opened(client, version=1)
    frame = await until(ws, 'push')
    assert frame['text'] == 'queued while offline' and frame['origin'] == 'cron'
    await ws.close()


@pytest.mark.asyncio
async def test_cron_push_during_active_turn_is_not_swallowed(server):
    adapter, client = server
    v1 = await opened(client, version=1)
    conn = adapter.registry.live('alice')[0]
    adapter._active_turn['alice'] = (conn, 't1')
    await adapter.send('alice', 'cron says hi', metadata={'job_id': 'j2'})
    frame = await until(v1, 'push')
    assert frame['text'] == 'cron says hi' and frame['origin'] == 'cron'
    await v1.close()


@pytest.mark.asyncio
async def test_cron_push_during_active_turn_reaches_v0_as_response(server):
    adapter, client = server
    v0 = await opened(client, version=0)
    await v0.send_json({'type': 'ping'})  # first non-hello frame settles v0 and flushes
    await until(v0, 'pong')
    conn = adapter.registry.live('alice')[0]
    adapter._active_turn['alice'] = (conn, 't1')
    await adapter.send('alice', 'cron for v0', metadata={'job_id': 'j3'})
    assert (await until(v0, 'response'))['text'] == 'cron for v0'
    await v0.close()


@pytest.mark.asyncio
async def test_final_reply_is_queued_when_asking_device_went_offline(server):
    adapter, client = server
    ws = await opened(client, version=1)
    conn = adapter.registry.live('alice')[0]
    adapter._active_turn['alice'] = (conn, 't1')
    await ws.close()
    for _ in range(50):  # let the server notice the close
        if not adapter.registry.live('alice'):
            break
        await asyncio.sleep(0.02)
    await adapter.send('alice', 'the final answer', metadata={'notify': True})
    assert [item[1] for item in adapter.outbox.drain('alice')] == ['the final answer']


@pytest.mark.asyncio
async def test_text_approval_prompt_reaches_v0_and_v1(server):
    adapter, client = server
    from glasses_plugin.devices import parse_devices
    adapter._tokens = parse_devices('alice:ray=tok-alice-1,alice:phone=tok-alice-2', '')
    v1 = await opened(client, 'tok-alice-1', version=1)
    v0 = await opened(client, 'tok-alice-2', version=0)
    await v0.send_json({'type': 'ping'})
    await until(v0, 'pong')
    conn = next(c for c in adapter.registry.live('alice') if c.protocol == 1)
    adapter._active_turn['alice'] = (conn, 't1')
    meta = {'_interim_send': True, 'is_approval_prompt': True}
    await adapter.send('alice', 'Approve running rm? Reply /approve or /deny', metadata=meta)
    assert (await until(v1, 'push'))['origin'] == 'approval'
    assert 'approve' in (await until(v0, 'response'))['text']
    await v1.close(); await v0.close()
