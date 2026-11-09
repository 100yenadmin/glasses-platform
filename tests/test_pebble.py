import asyncio
import json
import logging
from uuid import UUID

from aiohttp import ClientSession, WSMsgType
import pytest

from glasses_plugin import pebble
from glasses_plugin.devices import parse_devices
from gateway.platforms.event import ProcessingOutcome

pytestmark = pytest.mark.asyncio
QUERY = '/bobby/tok-alice-1/query'
QUOTA = '/bobby/tok-alice-1/quota'


def api_for(adapter):
    return next(route.handler.__self__ for route in adapter._app.router.routes()
                if route.resource.canonical == '/bobby/{token}/query')


async def read_turn(client, prompt='Hello', **params):
    ws = await client.ws_connect(QUERY, params={'prompt': prompt, **params}, headers={'Origin': 'null'})
    messages = []
    while True:
        frame = await ws.receive(timeout=2)
        if frame.type == WSMsgType.CLOSE:
            assert frame.data == 1000
            break
        assert frame.type == WSMsgType.TEXT
        messages.append(frame.data)
    assert ws.close_code == 1000
    return messages


def reply_with(adapter, monkeypatch, text='Hello back.'):
    async def handle(event):
        await adapter.on_processing_start(event)
        await adapter.send(event.source.chat_id, text, metadata={'notify': True})
        await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    monkeypatch.setattr(adapter, 'handle_message', handle)


async def test_bad_tokens_use_close_reason_and_quota_401(server):
    _, client = server
    # The timeline token is ignored, and cannot override an invalid path credential.
    ws = await client.ws_connect('/bobby/wrong/query?prompt=Hi&token=tok-alice-1')
    assert (await ws.receive()).data == 'd'
    close = await ws.receive()
    assert close.type == WSMsgType.CLOSE and close.data == 1008 and close.extra == 'Unauthorized'
    assert (await client.get('/bobby/wrong/quota?token=tok-alice-1')).status == 401


async def test_quota_is_bobby_compatible(server):
    _, client = server
    response = await client.get(QUOTA, params={'token': 'pebble-timeline-token'})
    assert response.status == 200
    assert await response.json() == {'used': 0, 'remaining': 1000000, 'hasSubscription': True}


async def test_query_v1_text_thread_and_close_sequence(server, monkeypatch):
    adapter, client = server
    seen = []

    async def query(conn, frame):
        assert conn.protocol == 1 and conn.capabilities == {}
        assert conn.flushed and frame['id'] in conn.asking
        assert frame['type'] == 'query' and frame['text'] == '雪 & tea?'
        seen.append(frame['id'])
        await adapter._deliver_reply('alice', conn, frame['id'], ' Warm tea. ')
    monkeypatch.setattr(adapter, '_process_query', query)
    result = await read_turn(client, '雪 & tea?', token='not-our-token', threadId='anything',
                             location='unknown', actions='timer', widgets='timer', lang='ja')
    assert result == ['fThinking…', 'c Warm tea. ', 'd', 't' + seen[0]]
    assert str(UUID(seen[0])) == seen[0]
    assert not adapter.registry.live('alice')


async def test_cumulative_deltas_stream_before_final_without_repeats(server, monkeypatch):
    adapter, client = server
    resume = asyncio.Event()

    async def query(conn, frame):
        await conn.send_json({'type': 'response_delta', 'turn': 'other', 'text': 'private'})
        await conn.send_json({'type': 'response_delta', 'turn': frame['id'], 'text': ' Hello'})
        await resume.wait()
        await conn.send_json({'type': 'response_delta', 'turn': frame['id'], 'text': ' Hello world'})
        await conn.send_json({'type': 'response_delta', 'turn': frame['id'], 'text': ' Hello world'})
        await adapter._deliver_reply('alice', conn, frame['id'], ' Hello world! ')
    monkeypatch.setattr(adapter, '_process_query', query)
    ws = await client.ws_connect(QUERY, params={'prompt': 'hi'})
    assert (await ws.receive()).data == 'fThinking…'
    assert (await ws.receive()).data == 'c Hello'
    assert not resume.is_set()  # a chunk was sent while Hermes was still running
    await ws.send_str('{"ignored":"action reply"}')
    resume.set()
    result = [message.data async for message in ws]
    assert result[:3] == ['c world', 'c! ', 'd']
    assert result[3].startswith('t') and len(result) == 4 and ws.close_code == 1000


async def test_new_stream_segment_is_forwarded(server, monkeypatch):
    adapter, client = server

    async def query(conn, frame):
        for text in ['Looking.', 'The answer', 'The answer is 42.']:
            await conn.send_json({'type': 'response_delta', 'turn': frame['id'], 'text': text})
        await adapter._deliver_reply('alice', conn, frame['id'], 'The answer is 42.')
    monkeypatch.setattr(adapter, '_process_query', query)
    result = await read_turn(client)
    assert [part for part in result if part.startswith('c')] == ['cLooking.', 'c\n\nThe answer', 'c is 42.']


async def test_streaming_cursor_never_resends_text(server, monkeypatch):
    adapter, client = server

    async def query(conn, frame):
        for text in ['Hello ▉', 'Hello world ▉']:  # Hermes' partial updates end in its streaming cursor
            await conn.send_json({'type': 'response_delta', 'turn': frame['id'], 'text': text})
        await adapter._deliver_reply('alice', conn, frame['id'], 'Hello world!')
    monkeypatch.setattr(adapter, '_process_query', query)
    result = await read_turn(client)
    assert ''.join(part[1:] for part in result if part.startswith('c')) == 'Hello world!'


async def test_delta_arriving_during_socket_send_is_not_skipped(server, monkeypatch):
    adapter, client = server
    resume = asyncio.Event()
    connection = []
    send = pebble.web.WebSocketResponse.send_str

    async def query(conn, frame):
        connection.append((conn, frame['id']))
        await conn.send_json({'type': 'response_delta', 'turn': frame['id'], 'text': 'Hello'})
        await resume.wait()
        await adapter._deliver_reply('alice', conn, frame['id'], 'Hello world!')

    async def concurrent(ws, data, *args, **kwargs):
        await send(ws, data, *args, **kwargs)
        if data == 'cHello':
            conn, turn_id = connection[0]
            await conn.send_json({'type': 'response_delta', 'turn': turn_id, 'text': 'Hello world'})
    monkeypatch.setattr(adapter, '_process_query', query)
    monkeypatch.setattr(pebble.web.WebSocketResponse, 'send_str', concurrent)
    ws = await client.ws_connect(QUERY, params={'prompt': 'hi'})
    try:
        assert (await ws.receive()).data == 'fThinking…'
        assert (await ws.receive()).data == 'cHello'
        assert (await ws.receive(timeout=0.25)).data == 'c world'
        resume.set()
        result = [message.data async for message in ws]
        assert result[:2] == ['c!', 'd'] and result[2].startswith('t')
        assert ws.close_code == 1000
    finally:
        resume.set()
        await ws.close()


async def test_widgets_are_stripped_even_across_stream_boundaries(server, monkeypatch):
    adapter, client = server
    widget = '<<!!WIDGET:{"timer":\n10}!!>>'
    await adapter.send('alice', 'Later' + widget, metadata={'origin': 'cron'})

    async def query(conn, frame):
        for text in ['Hi <', 'Hi <<!!WID', 'Hi <<!!WIDGET:{"timer":', 'Hi ' + widget,
                     'Hi ' + widget + ' there']:
            await conn.send_json({'type': 'response_delta', 'turn': frame['id'], 'text': text})
        await adapter._deliver_reply('alice', conn, frame['id'], 'Hi ' + widget + ' there!')
    monkeypatch.setattr(adapter, '_process_query', query)
    result = await read_turn(client)
    assert [part for part in result if part.startswith('c')] == [
        'cHi ', 'c there', 'c!', 'c\n\nReminder: Later']
    assert 'WIDGET' not in ''.join(result)


@pytest.mark.parametrize('prompt', [None, '', ' \n '])
async def test_missing_or_blank_prompt_warns_then_finishes(server, monkeypatch, prompt):
    adapter, client = server

    async def forbidden(*args):
        pytest.fail('blank question reached Hermes')
    monkeypatch.setattr(adapter, '_process_query', forbidden)
    params = {} if prompt is None else {'prompt': prompt}
    ws = await client.ws_connect(QUERY, params=params)
    assert [message.data async for message in ws] == ['wNo question.', 'd']
    assert ws.close_code == 1000 and not adapter.registry.live('alice')


@pytest.mark.parametrize('kind', ['approval', 'clarify', 'approval_request'])
async def test_prompts_finish_as_text_without_actions(server, monkeypatch, kind):
    adapter, client = server

    async def query(conn, frame):
        if kind == 'approval_request':
            await conn.send_json({'type': kind, 'text': 'Allow?', 'choices': ['yes', 'no']})
        else:
            await adapter.send('alice', 'Allow?', metadata={'glasses_prompt': kind})
    monkeypatch.setattr(adapter, '_process_query', query)
    result = await read_turn(client)
    assert result[:3] == ['fThinking…', 'cAllow?', 'd']
    assert result[3].startswith('t') and len(result) == 4


async def test_wait_setting_default_config_and_environment(make_adapter, monkeypatch):
    assert pebble.BobbyApi(make_adapter()).wait == 45
    assert pebble.BobbyApi(make_adapter(bobby_wait=0.125)).wait == 0.125
    monkeypatch.setenv('GLASSES_BOBBY_WAIT', '0.25')
    assert pebble.BobbyApi(make_adapter()).wait == 0.25


async def test_timeout_then_late_reply_is_shown_with_next_answer(server, monkeypatch):
    adapter, client = server
    api_for(adapter).wait = 0.01
    resume, finished = asyncio.Event(), asyncio.Event()

    async def handle(event):
        await adapter.on_processing_start(event)
        await resume.wait()
        await adapter.send('alice', 'Late answer', metadata={'notify': True})
        await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
        finished.set()
    monkeypatch.setattr(adapter, 'handle_message', handle)
    result = await read_turn(client, 'Slow')
    assert result[:3] == ['fThinking…', 'cStill working; ask again in a moment for the answer.', 'd']
    assert result[3].startswith('t') and not adapter.registry.live('alice')
    resume.set()
    await asyncio.wait_for(finished.wait(), 2)
    reply_with(adapter, monkeypatch, 'New answer')
    result = await read_turn(client, 'Again')
    assert result[1:4] == ['cNew answer', 'c\n\nReply: Late answer', 'd']
    assert not adapter.outbox.drain('alice')


async def test_push_is_durable_until_done_and_other_user_is_never_shown(server, monkeypatch):
    adapter, client = server
    adapter._tokens = parse_devices('alice:ray=tok-alice-1,bob:watch=tok-bob-1', '')
    await adapter.send('bob', 'private')
    await adapter.send('alice', 'Take tea', metadata={'origin': 'cron'})
    reply_with(adapter, monkeypatch, 'Answer')
    send = pebble.web.WebSocketResponse.send_str
    checked = []

    async def observed(ws, data, *args, **kwargs):
        if data == 'd':
            persisted = json.loads(adapter.outbox.path.read_text())
            assert persisted['alice'][0][1] == 'Take tea'
            assert not adapter._delivered.get('alice')
            checked.append(True)
        await send(ws, data, *args, **kwargs)
    monkeypatch.setattr(pebble.web.WebSocketResponse, 'send_str', observed)
    result = await read_turn(client)
    assert result[1:4] == ['cAnswer', 'c\n\nReminder: Take tea', 'd']
    assert checked == [True] and 'private' not in ''.join(result)
    assert not adapter.outbox.drain('alice')
    assert 'alice' not in json.loads(adapter.outbox.path.read_text())
    assert [item[1] for item in adapter.outbox.drain('bob')] == ['private']


@pytest.mark.parametrize('fail_at', ['cAnswer', 'c\n\nReminder: queued', 'd'])
async def test_send_failure_before_done_requeues_push_and_completed_reply(server, monkeypatch, fail_at):
    adapter, client = server
    await adapter.send('alice', 'queued', metadata={'origin': 'cron'})
    reply_with(adapter, monkeypatch, 'Answer')
    send = pebble.web.WebSocketResponse.send_str
    finished = asyncio.Event()
    requeue = pebble._requeue

    async def broken(ws, data, *args, **kwargs):
        if data == fail_at:
            raise ConnectionResetError('gone')
        await send(ws, data, *args, **kwargs)

    def observe(*args, **kwargs):
        requeue(*args, **kwargs)
        finished.set()
    monkeypatch.setattr(pebble.web.WebSocketResponse, 'send_str', broken)
    monkeypatch.setattr(pebble, '_requeue', observe)
    ws = await client.ws_connect(QUERY, params={'prompt': 'hi'})
    await asyncio.wait_for(finished.wait(), 2)
    queued = [item[1] for item in adapter.outbox.drain('alice')]
    assert queued == ['queued', 'Answer']
    assert not adapter._delivered.get('alice')
    await ws.close()


async def test_disconnect_mid_stream_requeues_push_and_late_reply(server, monkeypatch):
    adapter, client = server
    await adapter.send('alice', 'queued')
    resume, finished, requeued = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requeue = pebble._requeue

    async def handle(event):
        await adapter.on_processing_start(event)
        await adapter.send('alice', 'Partial', metadata={'expect_edits': True})
        await resume.wait()
        await adapter.send('alice', 'Whole reply', metadata={'notify': True})
        await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
        finished.set()

    def observe(*args, **kwargs):
        requeue(*args, **kwargs)
        requeued.set()
    monkeypatch.setattr(adapter, 'handle_message', handle)
    monkeypatch.setattr(pebble, '_requeue', observe)
    ws = await client.ws_connect(QUERY, params={'prompt': 'hi'})
    assert (await ws.receive()).data == 'fThinking…'
    assert (await ws.receive()).data == 'cPartial'
    await ws.close()
    await asyncio.wait_for(requeued.wait(), 2)
    assert not adapter.registry.live('alice')
    resume.set()
    await asyncio.wait_for(finished.wait(), 2)
    assert [item[1] for item in adapter.outbox.drain('alice')] == ['queued', 'Whole reply']


async def test_failure_after_done_keeps_push_delivered(server, monkeypatch):
    adapter, client = server
    await adapter.send('alice', 'queued')
    reply_with(adapter, monkeypatch, 'Answer')
    send = pebble.web.WebSocketResponse.send_str
    finished = asyncio.Event()
    requeue = pebble._requeue

    async def broken(ws, data, *args, **kwargs):
        if data.startswith('t'):
            raise ConnectionResetError('gone after done')
        await send(ws, data, *args, **kwargs)

    def observe(*args, **kwargs):
        requeue(*args, **kwargs)
        finished.set()
    monkeypatch.setattr(pebble.web.WebSocketResponse, 'send_str', broken)
    monkeypatch.setattr(pebble, '_requeue', observe)
    ws = await client.ws_connect(QUERY, params={'prompt': 'hi'})
    await asyncio.wait_for(finished.wait(), 2)
    assert not adapter.outbox.drain('alice') and adapter._delivered['alice']
    await ws.close()


async def test_error_is_warning_then_done(server, monkeypatch):
    adapter, client = server

    async def query(conn, frame):
        await conn.send_json({'type': 'error', 'message': 'Try again.'})
    monkeypatch.setattr(adapter, '_process_query', query)
    result = await read_turn(client)
    assert result[:3] == ['fThinking…', 'wTry again.', 'd']
    assert result[3].startswith('t') and len(result) == 4


@pytest.mark.parametrize('kind', ['response', 'approval'])
async def test_final_and_prompt_widgets_removed_but_ordinary_text_kept(server, monkeypatch, kind):
    adapter, client = server
    text = 'A<<!!WIDGET:{"n":1}!!>> B <ordinary> <<!!WID'

    async def query(conn, frame):
        if kind == 'response':
            await adapter._deliver_reply('alice', conn, frame['id'], text)
        else:
            await adapter.send('alice', text, metadata={'glasses_prompt': kind})
    monkeypatch.setattr(adapter, '_process_query', query)
    result = await read_turn(client)
    assert result[1:3] == ['cA B <ordinary> <<!!WID', 'd']


async def test_no_path_token_or_prompt_in_info_logs_on_real_listener(server, monkeypatch, caplog):
    adapter, _ = server
    reply_with(adapter, monkeypatch)
    caplog.set_level(logging.INFO)
    port = adapter._runner.addresses[0][1]
    async with ClientSession() as client:
        async with client.ws_connect(f'http://127.0.0.1:{port}' + QUERY,
                                     params={'prompt': 'private question for logs'}) as ws:
            async for _ in ws:
                pass
        assert (await client.get(f'http://127.0.0.1:{port}' + QUOTA)).status == 200
    assert 'tok-alice-1' not in caplog.text
    assert 'private question for logs' not in caplog.text


@pytest.mark.parametrize('deltas,final,shown', [
    ([' Hello world ▉'], 'Hello world', ' Hello world'),  # Hermes strips the final reply's whitespace
    (['Hello world ▉'], 'Hello', 'Hello world'),  # a shorter final is already on the watch
    (['Looking.', 'The answer'], 'The answer is 42.', 'Looking.\n\nThe answer is 42.'),
])
async def test_final_reply_never_repeats_streamed_text(server, monkeypatch, deltas, final, shown):
    adapter, client = server

    async def query(conn, frame):
        for text in deltas:
            await conn.send_json({'type': 'response_delta', 'turn': frame['id'], 'text': text})
        await adapter._deliver_reply('alice', conn, frame['id'], final)
    monkeypatch.setattr(adapter, '_process_query', query)
    result = await read_turn(client)
    assert ''.join(part[1:] for part in result if part.startswith('c')) == shown
