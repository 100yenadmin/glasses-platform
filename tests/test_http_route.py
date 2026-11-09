import asyncio
from unittest.mock import AsyncMock

import pytest

from glasses_plugin import http_api
from glasses_plugin.devices import parse_devices
from gateway.platforms.event import ProcessingOutcome

pytestmark = pytest.mark.asyncio
AUTH = {'Authorization': 'Bearer tok-alice-1'}


def api_for(adapter):
    return next(route.handler.__self__ for route in adapter._app.router.routes()
                if route.resource.canonical == '/turn')


def reply_with(adapter, text='Hello back.'):
    async def handle(event):
        await adapter.on_processing_start(event)
        await adapter.send(event.source.chat_id, text, metadata={'notify': True})
        await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    adapter.handle_message = AsyncMock(side_effect=handle)


def watch_connections(adapter, monkeypatch):
    connections = asyncio.Queue()
    add = adapter.registry.add

    async def watched(conn):
        await add(conn)
        connections.put_nowait(conn)
    monkeypatch.setattr(adapter.registry, 'add', watched)
    return connections


@pytest.mark.parametrize('path', ['/turn', '/pending', '/approval'])
@pytest.mark.parametrize('headers,query', [({}, ''), ({}, '?token=wrong'),
                                         ({'Authorization': 'Bearer wrong'}, ''),
                                         ({'Authorization': 'Bearer wrong'}, '?token=tok-alice-1')])
async def test_auth_rejected(server, path, headers, query):
    _, client = server
    request = client.get if path == '/pending' else client.post
    response = await request(path + query, headers=headers)
    assert response.status == 401
    assert await response.json() == {'error': 'unauthorized'}


@pytest.mark.parametrize('headers,query', [(AUTH, ''), ({}, '?token=tok-alice-1'),
                                         ({'Authorization': 'bEaReR tok-alice-1'}, '')])
async def test_text_turn_auth_and_existing_reply_path(server, headers, query):
    adapter, client = server
    reply_with(adapter)
    response = await client.post('/turn' + query, headers=headers, json={'text': 'Hello', 'id': 't1'})
    assert response.status == 200
    assert await response.json() == {'id': 't1', 'status': 'done', 'text': 'Hello back.', 'pushes': []}
    assert adapter.handle_message.call_args.args[0].message_id == 't1'
    assert adapter.registry.live('alice') == []


@pytest.mark.parametrize('raw', ['', '{', '[]', 'null', '1', '"hello"', '{}',
                                 '{"text":" "}', '{"text":3}', '{"audio":[]}'])
async def test_invalid_turn_body(server, raw):
    adapter, client = server
    adapter._process_query = AsyncMock()
    response = await client.post('/turn', headers=AUTH, data=raw)
    assert response.status == 400
    assert 'error' in await response.json()
    adapter._process_query.assert_not_called()
    assert not adapter.registry.live('alice')


async def test_body_size_limit_is_preserved(server):
    adapter, client = server
    adapter._app._client_max_size = 64
    response = await client.post('/turn', headers=AUTH, json={'text': 'x' * 100})
    assert response.status == 413


@pytest.mark.parametrize('capabilities', [None, [], {'buttons': True}])
async def test_generated_id_v1_capabilities_and_flush_before_query(server, monkeypatch, capabilities):
    adapter, client = server
    await adapter.send('alice', 'queued')
    seen = []

    async def query(conn, frame):
        assert conn.protocol == 1
        assert conn.capabilities == (capabilities if isinstance(capabilities, dict) else {})
        assert frame['id'] in conn.asking
        assert conn.flushed
        assert conn.ws.frames[0]['text'] == 'queued'
        seen.append(frame)
        await adapter._deliver_reply('alice', conn, frame['id'], 'answer')
    monkeypatch.setattr(adapter, '_process_query', query)
    response = await client.post('/turn', headers=AUTH, json={'text': 'Hi', 'capabilities': capabilities})
    result = await response.json()
    assert response.status == 200
    assert result['id'] == seen[0]['id'] and result['id']
    assert [item['text'] for item in result['pushes']] == ['queued']
    assert adapter.outbox.drain('alice') == []


@pytest.mark.parametrize('kind', ['approval', 'clarify', 'approval_request'])
async def test_prompt_ends_request_but_keeps_turn_running(server, monkeypatch, kind):
    adapter, client = server
    resume, finished = asyncio.Event(), asyncio.Event()

    async def handle(event):
        await adapter.on_processing_start(event)
        if kind == 'approval_request':
            await event._glasses_connection.send_json({'type': kind, 'id': 'a1', 'text': 'Allow?'})
        else:
            await adapter.send('alice', 'Allow?', metadata={'glasses_prompt': kind})
        await resume.wait()
        await adapter.send('alice', 'Finished', metadata={'notify': True})
        await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
        finished.set()
    monkeypatch.setattr(adapter, 'handle_message', handle)
    response = await client.post('/turn', headers=AUTH, json={'text': 'Do it', 'id': 't1'})
    result = await response.json()
    assert response.status == 200 and result['status'] == 'needs_answer'
    assert [item['text'] for item in result['prompts']] == ['Allow?']
    assert result['pushes'] == []
    assert not finished.is_set()
    resume.set()
    await asyncio.wait_for(finished.wait(), 2)
    pending = await client.get('/pending?wait=0', headers=AUTH)
    items = (await pending.json())['items']
    assert [(item['origin'], item['text']) for item in items] == [('reply', 'Finished')]


async def test_timeout_late_reply_is_queued_then_polled(server, monkeypatch):
    adapter, client = server
    api_for(adapter).turn_timeout = 0.01
    resume, finished = asyncio.Event(), asyncio.Event()

    async def handle(event):
        await adapter.on_processing_start(event)
        await resume.wait()
        await adapter.send('alice', 'Late reply', metadata={'notify': True})
        await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
        finished.set()
    monkeypatch.setattr(adapter, 'handle_message', handle)
    response = await client.post('/turn', headers=AUTH, json={'text': 'Slow', 'id': 'slow'})
    assert response.status == 202
    assert await response.json() == {'id': 'slow', 'status': 'pending', 'pushes': []}
    assert not adapter.registry.live('alice')
    resume.set()
    await asyncio.wait_for(finished.wait(), 2)
    response = await client.get('/pending?wait=0', headers=AUTH)
    items = (await response.json())['items']
    assert [(item['origin'], item['text']) for item in items] == [('reply', 'Late reply')]
    assert (await (await client.get('/pending?wait=0', headers=AUTH)).json())['items'] == []


async def test_turn_timeout_setting_default_config_and_environment(make_adapter, monkeypatch):
    assert http_api.HttpApi(make_adapter()).turn_timeout == 50
    assert http_api.HttpApi(make_adapter(turn_timeout=0.125)).turn_timeout == 0.125
    monkeypatch.setenv('GLASSES_TURN_TIMEOUT', '0.25')
    assert http_api.HttpApi(make_adapter()).turn_timeout == 0.25


async def test_pending_queued_push_and_user_isolation(server):
    adapter, client = server
    adapter._tokens = parse_devices('alice:ray=tok-alice-1,bob:watch=tok-bob-1', '')
    await adapter.send('bob', 'private')
    await adapter.send('alice', 'first')
    await adapter.send('alice', 'second', metadata={'origin': 'cron'})
    response = await asyncio.wait_for(client.get('/pending', headers=AUTH), 2)
    assert response.status == 200
    assert [item['text'] for item in (await response.json())['items']] == ['first', 'second']
    assert [item[1] for item in adapter.outbox.drain('bob')] == ['private']


@pytest.mark.parametrize('kind', ['push', 'approval_request'])
async def test_pending_waits_for_live_push_and_drops_status(server, monkeypatch, kind):
    adapter, client = server
    connections = watch_connections(adapter, monkeypatch)
    checked_status = asyncio.Event()
    wait_for = http_api._wait_for

    async def wait(socket, ready, seconds):
        def checked(frames):
            if any(item['type'] == 'status' for item in frames):
                checked_status.set()
            return ready(frames)
        await wait_for(socket, checked, seconds)
    monkeypatch.setattr(http_api, '_wait_for', wait)
    request = asyncio.create_task(client.get('/pending?wait=2', headers=AUTH))
    conn = await asyncio.wait_for(connections.get(), 2)
    await conn.send_json({'type': 'status', 'state': 'thinking'})
    await conn.send_json({'type': 'response_delta', 'turn': 'other', 'text': 'preview'})
    await asyncio.wait_for(checked_status.wait(), 2)
    assert not request.done()
    if kind == 'push':
        await adapter.send('alice', 'live')
    else:
        await conn.send_json({'type': 'approval_request', 'id': 'a1', 'text': 'Allow?'})
    response = await asyncio.wait_for(request, 2)
    items = (await response.json())['items']
    assert len(items) == 1 and items[0]['type'] == kind


async def test_pending_deadline_and_status_only(server, monkeypatch):
    adapter, client = server
    connections = watch_connections(adapter, monkeypatch)
    request = asyncio.create_task(client.get('/pending?wait=0.01', headers=AUTH))
    conn = await asyncio.wait_for(connections.get(), 2)
    await conn.send_json({'type': 'status', 'state': 'idle'})
    response = await asyncio.wait_for(request, 2)
    assert await response.json() == {'items': []}
    assert not adapter.registry.live('alice')


@pytest.mark.parametrize('value,expected', [('', 25), ('?wait=-10', 0), ('?wait=99', 55), ('?wait=0', 0)])
async def test_pending_default_and_clamped_wait(server, monkeypatch, value, expected):
    _, client = server
    waits = []

    async def wait(socket, ready, seconds):
        waits.append(seconds)
    monkeypatch.setattr(http_api, '_wait_for', wait)
    response = await client.get('/pending' + value, headers=AUTH)
    assert await response.json() == {'items': []}
    assert waits == [expected]


@pytest.mark.parametrize('value', ['bad', 'nan'])
async def test_pending_invalid_wait(server, value):
    _, client = server
    assert (await client.get('/pending?wait=' + value, headers=AUTH)).status == 400


async def test_same_device_replaces_pending_and_wakes_it(server, monkeypatch):
    adapter, client = server
    connections = watch_connections(adapter, monkeypatch)
    first = asyncio.create_task(client.get('/pending?wait=55', headers=AUTH))
    old = await asyncio.wait_for(connections.get(), 2)
    second = await client.get('/pending?wait=0', headers=AUTH)
    response = await asyncio.wait_for(first, 2)
    assert old.ws.closed
    assert await response.json() == await second.json() == {'items': []}


@pytest.mark.parametrize('position', ['before_close', 'during_close'])
@pytest.mark.parametrize('path', ['/turn', '/pending?wait=0'])
async def test_push_at_finish_is_buffered_or_queued_never_lost(server, monkeypatch, position, path):
    adapter, client = server
    reply_with(adapter)
    push_tasks = []
    if position == 'before_close':
        finish = http_api._finish

        async def boundary(adapter, conn):
            await adapter.send('alice', 'boundary push', metadata={'origin': 'cron'})
            return await finish(adapter, conn)
        monkeypatch.setattr(http_api, '_finish', boundary)
    else:
        close = http_api.HttpSocket.close

        async def boundary(socket, code=1000, message=b''):
            await close(socket, code, message)
            # _finish holds the user lock. Queue a real push on that lock before its snapshot.
            push_tasks.append(asyncio.create_task(adapter.send('alice', 'boundary push', metadata={'origin': 'cron'})))
            await asyncio.sleep(0)
            assert not push_tasks[-1].done()
        monkeypatch.setattr(http_api.HttpSocket, 'close', boundary)
    response = await (client.post(path, headers=AUTH, json={'text': 'Hello'}) if path == '/turn'
                      else client.get(path, headers=AUTH))
    await asyncio.gather(*push_tasks)
    result = await response.json()
    buffered = result.get('pushes', result.get('items', []))
    queued = adapter.outbox.drain('alice')
    assert sum(item['text'] == 'boundary push' for item in buffered) + sum(
        item[1] == 'boundary push' for item in queued) == 1
    assert bool(buffered) == (position == 'before_close')


@pytest.mark.parametrize('outcome,expected,status', [('reply', 'Hello back.', 200),
                                                   ('prompt', 'Allow?', 200), ('timeout', '', 202)])
async def test_plain_text(server, monkeypatch, outcome, expected, status):
    adapter, client = server
    if outcome == 'reply':
        reply_with(adapter)
    elif outcome == 'prompt':
        async def query(conn, frame):
            await adapter.send('alice', 'Allow?', metadata={'glasses_prompt': 'approval'})
        monkeypatch.setattr(adapter, '_process_query', query)
    else:
        api_for(adapter).turn_timeout = 0.01
        monkeypatch.setattr(adapter, '_process_query', AsyncMock())
    response = await client.post('/turn', headers={**AUTH, 'Accept': 'text/plain'}, json={'text': 'Hello'})
    assert response.status == status and response.content_type == 'text/plain'
    assert await response.text() == expected


@pytest.mark.parametrize('terminal,expected', [({'type': 'error', 'message': 'Could not hear.'}, 'Could not hear.'),
                                            ({'type': 'session_reset'}, '')])
async def test_error_and_session_reset_end_turn(server, monkeypatch, terminal, expected):
    adapter, client = server

    async def query(conn, frame):
        await conn.send_json(terminal)
    monkeypatch.setattr(adapter, '_process_query', query)
    response = await client.post('/turn', headers=AUTH, json={'text': 'Hello'})
    assert response.status == 200
    result = await response.json()
    assert result['status'] == 'done' and result['text'] == expected


async def test_audio_image_speak_forwarding_and_matching_transcript_audio(server, monkeypatch):
    adapter, client = server
    allow_audio = asyncio.Event()
    response_sent = asyncio.Event()
    body = {'id': 'voice', 'audio': {'data': 'voice-data', 'mime': 'audio/wav'},
            'image': {'data': 'photo-data', 'mime': 'image/jpeg'}, 'speak': True,
            'capabilities': {'audio_out': ['audio/mpeg']}}

    async def query(conn, frame):
        assert frame == {key: value for key, value in body.items() if key != 'capabilities'} | {'type': 'query'}
        await conn.send_json({'type': 'transcript', 'turn': 'other', 'text': 'wrong'})
        await conn.send_json({'type': 'transcript', 'turn': 'voice', 'text': 'heard words'})
        await conn.send_json({'type': 'response', 'turn': 'other', 'text': 'wrong'})
        await conn.send_json({'type': 'audio', 'ref': 'other', 'data': 'wrong', 'mime': 'audio/wav'})
        await conn.send_json({'type': 'response', 'turn': 'voice', 'text': 'answer'})
        response_sent.set()
        await allow_audio.wait()
        await conn.send_json({'type': 'audio', 'ref': 'voice', 'data': 'spoken-data', 'mime': 'audio/mpeg'})
    monkeypatch.setattr(adapter, '_process_query', query)
    request = asyncio.create_task(client.post('/turn', headers=AUTH, json=body))
    await asyncio.wait_for(response_sent.wait(), 2)
    assert not request.done()
    allow_audio.set()
    response = await asyncio.wait_for(request, 2)
    assert await response.json() == {'id': 'voice', 'status': 'done', 'text': 'answer', 'pushes': [],
                                     'transcript': 'heard words', 'audio': {'data': 'spoken-data', 'mime': 'audio/mpeg'}}


async def test_audio_wait_is_bounded_and_text_survives_missing_audio(server, monkeypatch):
    adapter, client = server
    reply_with(adapter)
    waits = []
    wait_for = http_api._wait_for

    async def wait(socket, ready, seconds):
        waits.append(seconds)
        await wait_for(socket, ready, 0.01 if seconds == 15 else seconds)
    monkeypatch.setattr(http_api, '_wait_for', wait)
    response = await client.post('/turn', headers=AUTH,
                                 json={'text': 'Hello', 'capabilities': {'audio_out': ['audio/mpeg']}})
    result = await response.json()
    assert result['status'] == 'done' and result['text'] == 'Hello back.' and 'audio' not in result
    assert waits == [50, 15]


async def test_approval_stub_and_hook(server, monkeypatch):
    adapter, client = server
    response = await client.post('/approval', headers=AUTH, json={'id': 'a1', 'choice': 'once'})
    assert response.status == 501 and await response.json() == {'error': 'not yet'}
    assert not adapter.registry.live('alice')

    async def approval(adapter_arg, conn, body):
        assert adapter_arg is adapter and conn in adapter.registry.live('alice')
        assert conn.protocol == 1 and conn.capabilities == {'buttons': True}
        assert body == {'id': 'a1', 'choice': 'deny', 'capabilities': {'buttons': True}}
        return http_api.web.json_response({'ok': True})
    monkeypatch.setattr(http_api, 'handle_approval', approval)
    response = await client.post('/approval', headers=AUTH,
                                 json={'id': 'a1', 'choice': 'deny', 'capabilities': {'buttons': True}})
    assert await response.json() == {'ok': True}


@pytest.mark.parametrize('raw', ['', '[]', '{'])
async def test_approval_body_validation(server, raw):
    _, client = server
    assert (await client.post('/approval', headers=AUTH, data=raw)).status == 400


async def test_socket_close_fails_sends_and_wakes_waiter():
    socket = http_api.HttpSocket()
    wait = asyncio.create_task(http_api._wait_for(socket, lambda frames: False, 55))
    await socket.close(code=4000, message=b'replaced')
    await asyncio.wait_for(wait, 1)
    with pytest.raises(ConnectionError):
        await socket.send_json({'type': 'push', 'text': 'too late'})


async def test_other_users_live_push_never_enters_turn(server, monkeypatch):
    adapter, client = server

    async def handle(event):
        await adapter.on_processing_start(event)
        await adapter.send('bob', 'private', metadata={'origin': 'cron'})
        await adapter.send('alice', 'ours', metadata={'origin': 'cron'})
        await adapter.send('alice', 'answer', metadata={'notify': True})
        await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    monkeypatch.setattr(adapter, 'handle_message', handle)
    response = await client.post('/turn', headers=AUTH, json={'text': 'Hello'})
    result = await response.json()
    assert result['text'] == 'answer'
    assert [item['text'] for item in result['pushes']] == ['ours']
    assert [item[1] for item in adapter.outbox.drain('bob')] == ['private']


async def test_finish_releases_pending_and_held_pushes(server):
    adapter, _ = server
    from glasses_plugin.devices import Connection, Identity
    conn = Connection(http_api.HttpSocket(), Identity('alice', 'ray'), protocol=1)
    await adapter.registry.add(conn)
    await adapter.send('alice', 'pending')  # not flushed yet
    conn.held = [(conn.pending[0][0], 'held', 'cron', 'held-id')]
    await http_api._finish(adapter, conn)
    assert conn.ws.closed and not adapter.registry.live('alice')
    assert conn.pending == conn.held == []
    assert [item[1] for item in adapter.outbox.drain('alice')] == ['pending', 'held']
