import asyncio
import base64
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from glasses_plugin.adapter import Turn
from glasses_plugin.devices import Connection, Identity
from gateway.platforms.event import MessageType, ProcessingOutcome

JPEG = Path(__file__).parent / 'fixtures' / 'scene.jpg'


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
async def test_user_and_device_routing(server):
    adapter, client = server
    from glasses_plugin.devices import parse_devices
    adapter._tokens = parse_devices('alice:ray=tok-alice-1,alice:phone=tok-alice-2,bob:g2=tok-bob-1', '')
    alice = await opened(client)
    phone = await opened(client, 'tok-alice-2')
    bob = await opened(client, 'tok-bob-1')
    await adapter.send('alice', 'push', metadata={'origin': 'cron'})
    assert (await until(alice, 'push'))['text'] == 'push'
    assert (await until(phone, 'push'))['origin'] == 'cron'
    # A ping sentinel proves no stray push was placed on Bob's connection.
    await bob.send_json({'type': 'ping'})
    assert (await bob.receive_json())['type'] == 'pong'
    conn = adapter.registry.live('alice')[0]
    adapter._active_turn['alice'] = Turn(conn, 't1')
    await adapter.send('alice', 'reply', metadata={'notify': True})
    await adapter.on_processing_complete(turn_event('t1'), ProcessingOutcome.SUCCESS)
    assert (await until(alice, 'response'))['turn'] == 't1'
    await phone.send_json({'type': 'ping'})
    assert (await phone.receive_json())['type'] == 'pong'
    await alice.close(); await phone.close(); await bob.close()


@pytest.mark.asyncio
async def test_offline_flush(server):
    adapter, client = server
    await adapter.send('alice', 'one')
    await adapter.send('alice', 'two')
    ws = await opened(client, version=0)
    assert [(await until(ws, 'response'))['text'] for _ in range(2)] == ['one', 'two']
    assert adapter.outbox.drain('alice') == []
    await ws.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('version', [0, 1])
async def test_photo_read_loop_and_event(server, version):
    adapter, client = server
    events = asyncio.Queue()
    adapter.handle_message = AsyncMock(side_effect=events.put)
    ws = await opened(client, version=version)
    await ws.send_json({'type': 'query', 'id': 'photo-turn', 'text': 'what am I looking at?'})
    capture = await until(ws, 'capture_photo')
    # Named deadlock probe: ping completes before the photo future is resolved.
    await ws.send_json({'type': 'ping'})
    await until(ws, 'pong')
    answer = {'type': 'photo', 'data': base64.b64encode(JPEG.read_bytes()).decode()}
    if version:
        await ws.send_json({**answer, 'request': 'wrong-id'})
        await ws.send_json({'type': 'ping'})
        await until(ws, 'pong')
        assert events.empty()
        answer['request'] = capture['request']
    await ws.send_json(answer)
    event = await asyncio.wait_for(events.get(), 3)
    assert event.message_type is MessageType.PHOTO
    assert event.media_types == ['image/jpeg']
    assert Path(event.media_urls[0]).read_bytes() == JPEG.read_bytes()
    assert event.source.chat_id == event.source.user_id == 'alice'
    assert 'image' not in event.raw_message and 'data' not in event.raw_message
    await ws.close()


@pytest.mark.asyncio
async def test_capture_timeout_and_no_camera(server):
    adapter, client = server
    events = asyncio.Queue()
    adapter.handle_message = AsyncMock(side_effect=events.put)
    ws = await opened(client)
    await ws.send_json({'type': 'query', 'text': 'read this'})
    await until(ws, 'capture_photo')
    event = await asyncio.wait_for(events.get(), 3)
    assert event.message_type is MessageType.TEXT
    assert event.text.startswith('(No photo could be captured from the glasses.) ')
    await ws.send_json({'type': 'hello', 'device': {'capabilities': {'camera': False}}})
    await until(ws, 'hello_ack')
    await ws.send_json({'type': 'query', 'text': 'read this'})
    event = await asyncio.wait_for(events.get(), 3)
    assert event.message_type is MessageType.TEXT
    await ws.send_json({'type': 'ping'})
    assert (await ws.receive_json())['type'] == 'pong'
    await ws.close()


@pytest.mark.asyncio
async def test_malformed_new_session_inline(server):
    adapter, client = server
    events = asyncio.Queue()
    adapter.handle_message = AsyncMock(side_effect=events.put)
    ws = await opened(client)
    for raw in ('bad', '[]', '{"type":[]}', '{"type":"future"}'):
        await ws.send_str(raw)
    await ws.send_bytes(b'ignored')
    await ws.send_json({'type': 'query', 'text': []})
    assert (await until(ws, 'error'))['message'] == 'Empty query.'
    await ws.send_json({'type': 'new_session'})
    reset = await asyncio.wait_for(events.get(), 3)
    assert reset.text == '/new'
    await adapter.on_processing_start(reset)
    await adapter.send('alice', 'New session started.', metadata={'notify': True})
    await adapter.on_processing_complete(reset, ProcessingOutcome.SUCCESS)
    assert (await until(ws, 'session_reset')) == {'type': 'session_reset'}
    await ws.send_json({'type': 'query', 'text': 'hello', 'image': {'data': base64.b64encode(JPEG.read_bytes()).decode()}})
    event = await asyncio.wait_for(events.get(), 3)
    assert event.message_type is MessageType.PHOTO
    assert 'image' not in event.raw_message
    await ws.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('version', [0, 1])
async def test_streaming_and_lifecycle(server, version):
    adapter, client = server
    ws = await opened(client, version=version)
    conn = adapter.registry.live('alice')[0]
    adapter._active_turn['alice'] = Turn(conn, 'turn')
    await adapter.send('alice', 'Let me look.', metadata={'_interim_send': True})
    first = await adapter.send('alice', 'a', metadata={'expect_edits': True})
    await adapter.edit_message('alice', first.message_id, 'ab')
    # Hermes finalizes progress-bubble edits and segment breaks too; neither may end the turn.
    await adapter.edit_message('alice', 'progress-bubble', 'tool line', finalize=True)
    await adapter.edit_message('alice', first.message_id, 'abc', finalize=True)
    if version:
        assert (await until(ws, 'status'))['detail'] == 'Let me look.'
        assert (await until(ws, 'response_delta'))['text'] == 'a'
        assert (await until(ws, 'response_delta'))['text'] == 'ab'
        assert (await until(ws, 'status'))['detail'] == 'tool line'
        assert (await until(ws, 'response_delta'))['text'] == 'abc'
    await adapter.on_processing_complete(turn_event('older'), ProcessingOutcome.SUCCESS)
    assert adapter._active_turn['alice'].id == 'turn'
    await adapter.on_processing_complete(turn_event('turn'), ProcessingOutcome.SUCCESS)
    assert 'alice' not in adapter._active_turn
    final = await ws.receive_json()  # v0: the very first frame after welcome is the one reply
    assert final['type'] == 'response' and final['text'] == 'abc'
    if version:
        assert final['turn'] == 'turn' and final['final'] is True
        assert (await until(ws, 'status'))['state'] == 'idle'
    await ws.close()


@pytest.mark.asyncio
async def test_health_has_no_identity(server):
    _, client = server
    response = await client.get('/health')
    assert await response.json() == {'ok': True}


@pytest.mark.asyncio
async def test_startup_replay_restores_route_and_notify_marks_final(server):
    adapter, client = server
    events = asyncio.Queue()
    adapter.handle_message = AsyncMock(side_effect=events.put)
    ws = await opened(client)
    await ws.send_json({'type': 'query', 'id': 'replayed', 'text': 'hello'})
    event = await asyncio.wait_for(events.get(), 3)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    assert not adapter._active_turn
    await adapter.on_processing_start(event)
    assert (await until(ws, 'status'))['state'] == 'idle'
    assert (await until(ws, 'status'))['state'] == 'thinking'
    await adapter.send('alice', 'preview', metadata={'expect_edits': True})
    assert (await until(ws, 'response_delta'))['text'] == 'preview'
    await adapter.send('alice', 'final', metadata={'notify': True})
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    final = await until(ws, 'response')
    assert final['turn'] == 'replayed' and final['text'] == 'final'
    assert adapter.config.home_channel.chat_id == 'alice'
    await ws.close()
