"""Regression tests for review findings F1–F4 (routing of finals, pushes, queued pushes and approval prompts)."""
import asyncio

import pytest
from gateway.platforms.event import ProcessingOutcome
from glasses_plugin.adapter import Turn


async def until(ws, kind):
    async with asyncio.timeout(3):
        while True:
            frame = await ws.receive_json()
            if frame['type'] == kind:
                return frame


def turn_event(message_id, user='alice'):
    return type('Event', (), {'source': type('Source', (), {'chat_id': user})(), 'message_id': message_id})()


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
    adapter._active_turn['alice'] = Turn(conn, 't1')
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
    adapter._active_turn['alice'] = Turn(conn, 't1')
    await adapter.send('alice', 'cron for v0', metadata={'job_id': 'j3'})
    assert (await until(v0, 'response'))['text'] == 'cron for v0'
    await v0.close()


@pytest.mark.asyncio
async def test_final_reply_is_queued_when_asking_device_went_offline(server):
    adapter, client = server
    ws = await opened(client, version=1)
    conn = adapter.registry.live('alice')[0]
    adapter._active_turn['alice'] = Turn(conn, 't1')
    await ws.close()
    for _ in range(50):  # let the server notice the close
        if not adapter.registry.live('alice'):
            break
        await asyncio.sleep(0.02)
    await adapter.send('alice', 'the final answer', metadata={'notify': True})
    await adapter.on_processing_complete(turn_event('t1'), ProcessingOutcome.SUCCESS)
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
    adapter._active_turn['alice'] = Turn(conn, 't1')
    meta = {'_interim_send': True, 'is_approval_prompt': True}
    await adapter.send('alice', 'Approve running rm? Reply /approve or /deny', metadata=meta)
    assert (await until(v1, 'push'))['origin'] == 'approval'
    assert 'approve' in (await until(v0, 'response'))['text']
    await v1.close(); await v0.close()


@pytest.mark.asyncio
async def test_clarify_question_reaches_v0_and_v1(server):
    adapter, client = server
    from glasses_plugin.devices import parse_devices
    adapter._tokens = parse_devices('alice:ray=tok-alice-1,alice:phone=tok-alice-2', '')
    v1 = await opened(client, 'tok-alice-1', version=1)
    v0 = await opened(client, 'tok-alice-2', version=0)
    await v0.send_json({'type': 'ping'})
    await until(v0, 'pong')
    conn = next(c for c in adapter.registry.live('alice') if c.protocol == 1)
    adapter._active_turn['alice'] = Turn(conn, 't1')
    # Hermes sends the text clarify prompt with no notify/interim marker, like a progress bubble.
    await adapter.send_clarify('alice', 'Which door?', None, 'clarify-1', 'session-1')
    push = await until(v1, 'push')
    assert push['origin'] == 'clarify' and 'Which door?' in push['text']
    assert 'Which door?' in (await until(v0, 'response'))['text']
    await v1.close(); await v0.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('sends, expected', [
    # Streamed: a one-shot preamble, then a streamed answer segment -> the last segment only.
    ([('Let me check.', {'notify': True}), ('The sign', {'expect_edits': True}), ('The sign says 42.', 'edit')],
     'The sign says 42.'),
    # Streamed answer whose final arrives as a fresh notify send -> replaces the preview.
    ([('The sign', {'expect_edits': True}), ('The sign says 42.', {'notify': True})], 'The sign says 42.'),
    # Not streamed: a final split into chunks -> all chunks, in order.
    ([('Part one.', {'notify': True}), ('Part two.', {'notify': True})], 'Part one.\n\nPart two.'),
])
async def test_one_reply_per_turn_from_segments(server, sends, expected):
    adapter, client = server
    ws = await opened(client, version=0)
    conn = adapter.registry.live('alice')[0]
    adapter._active_turn['alice'] = Turn(conn, 't1')
    stream_id = None
    for text, meta in sends:
        if meta == 'edit':
            await adapter.edit_message('alice', stream_id, text, finalize=True)
        else:
            result = await adapter.send('alice', text, metadata=meta)
            stream_id = result.message_id if meta.get('expect_edits') else stream_id
    await adapter.on_processing_complete(turn_event('t1'), ProcessingOutcome.SUCCESS)
    frame = await ws.receive_json()
    assert frame == {'type': 'response', 'text': expected, 'tts': False}
    await ws.close()


@pytest.mark.asyncio
async def test_typing_refresh_and_pauses_do_not_flap_status(server):
    adapter, client = server
    ws = await opened(client, version=1)
    conn = adapter.registry.live('alice')[0]
    adapter._active_turn['alice'] = Turn(conn, 't1')
    await adapter.send_typing('alice')
    await adapter.send('alice', 'web_search: glasses', metadata=None)
    await adapter.send_typing('alice')  # refresh tick
    await adapter.stop_typing('alice')  # mid-turn pause
    await adapter.send_typing('alice')
    await adapter.on_processing_complete(turn_event('t1'), ProcessingOutcome.SUCCESS)
    await adapter.stop_typing('alice')  # after the turn: idle already reported
    await ws.send_json({'type': 'ping'})
    frames = []
    while (frame := await ws.receive_json())['type'] != 'pong':
        frames.append(frame)
    assert frames == [{'type': 'status', 'state': 'thinking'},
                      {'type': 'status', 'state': 'working', 'detail': 'web_search: glasses'},
                      {'type': 'status', 'state': 'idle'}]
    await ws.close()
