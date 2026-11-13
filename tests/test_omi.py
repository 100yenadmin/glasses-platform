import asyncio
from unittest.mock import AsyncMock

import pytest

from glasses_plugin import http_api
from glasses_plugin.devices import parse_devices
from glasses_plugin.tests.test_http_route import reply_with, watch_connections

pytestmark = pytest.mark.asyncio
URL = '/omi/tok-alice-1/transcript?session_id=live&uid=anything'


def api_for(adapter):
    return next(route.handler.__self__ for route in adapter._app.router.routes()
                if route.resource.canonical == '/omi/{token}/transcript')


def segment(text, start=0, **extra):
    return {'text': text, 'start': start, 'end': start + 1, 'speaker': 'SPEAKER_00',
            'speakerId': 0, **extra}


async def post(client, *segments, url=URL):
    response = await client.post(url, json=list(segments))
    assert response.status == 200
    return await response.json()


async def test_bad_path_token_cannot_be_overridden(server):
    adapter, client = server
    adapter._process_query = AsyncMock()
    response = await client.post('/omi/wrong/transcript?token=tok-alice-1&uid=alice',
                                 headers={'Authorization': 'Bearer tok-alice-1'}, json=[])
    assert response.status == 401
    assert await response.json() == {'error': 'unauthorized'}
    adapter._process_query.assert_not_called()
    assert not adapter.registry.live('alice')


@pytest.mark.parametrize('text', ['talking to somebody', 'ask Hermes tomorrow', 'hermesian weather', 'hey hermesville',
                                 "Hermes's docs are great", 'Hermes’s docs are great',
                                 "Hermes 's docs are great", 'Hermes ’s docs are great',
                                 ])
async def test_no_wake_phrase_is_not_a_query(server, text):
    adapter, client = server
    adapter._process_query = AsyncMock()
    api_for(adapter).omi_wait = 0.01
    assert await post(client, segment(text)) == {}
    adapter._process_query.assert_not_called()
    assert not adapter.registry.live('alice')


@pytest.mark.parametrize('wake', ['Hey Hermes, ', 'hermes ', 'OK, HEY HERMES, ', 'Okay... Hermes, — ',
                                 '“Hey, Hermes,” ', 'Hey Hermes. ', 'Hey, Hermes? ', 'Hermes: ', 'OK, HEY HERMES! ',
                                 'hermes\t'])
async def test_wake_becomes_existing_v1_query_and_reply(server, monkeypatch, wake):
    adapter, client = server
    connections = watch_connections(adapter, monkeypatch)
    reply_with(adapter, 'It is noon.')
    assert await post(client, segment(wake + 'what time is it')) == {'message': 'Hermes: It is noon.'}
    event = adapter.handle_message.call_args.args[0]
    assert event.text == 'what time is it'
    assert event.source.chat_id == 'alice'  # uid is ignored
    conn = connections.get_nowait()
    assert conn.identity.device == 'ray' and conn.protocol == 1 and conn.capabilities == {}
    assert event.message_id and conn.ws.closed and not adapter.registry.live('alice')


async def test_wake_alone_collects_next_segments_in_same_session(server):
    adapter, client = server
    reply_with(adapter)
    assert await post(client, segment('Hey Hermes,')) == {}
    assert await post(client, segment('unrelated', 1), url=URL.replace('live', 'other')) == {}
    assert await post(client, segment('what time', 1), segment('is it', 2)) == {'message': 'Hermes: Hello back.'}
    assert adapter.handle_message.call_args.args[0].text == 'what time is it'
    assert await post(client, segment('just talking', 3)) == {}
    assert adapter.handle_message.await_count == 1


@pytest.mark.parametrize('delay,answered', [(8, True), (8.01, False)])
async def test_wake_alone_has_eight_second_deadline(server, monkeypatch, delay, answered):
    from glasses_plugin import omi
    adapter, client = server
    now = [10.0]
    monkeypatch.setattr(omi, 'monotonic', lambda: now[0])
    reply_with(adapter)
    assert await post(client, segment('Hermes')) == {}
    now[0] += delay
    result = await post(client, segment('tell me the time', 1))
    assert result == ({'message': 'Hermes: Hello back.'} if answered else {})
    assert adapter.handle_message.await_count == int(answered)


async def test_user_segments_win_when_present_and_fallback_includes_all_speakers(server):
    adapter, client = server
    reply_with(adapter)
    assert await post(client, segment('Hey Hermes, wrong', is_user=False),
                      segment('Hey Hermes, right', 1, is_user=True)) == {'message': 'Hermes: Hello back.'}
    assert adapter.handle_message.call_args.args[0].text == 'right'
    assert adapter.handle_message.await_count == 1
    assert await post(client, segment('Hey Hermes, fallback', 2, is_user=False)) == {'message': 'Hermes: Hello back.'}
    assert adapter.handle_message.call_args.args[0].text == 'fallback'


async def test_non_user_cannot_wake_or_complete_when_user_present(server):
    adapter, client = server
    reply_with(adapter)
    assert await post(client, segment('Hey Hermes', is_user=False), segment('hello', 1, is_user=True)) == {}
    assert await post(client, segment('question', 2)) == {}
    assert await post(client, segment('Hermes', 3, is_user=True)) == {}
    assert await post(client, segment('wrong', 4, is_user=False), segment('right', 5, is_user=True)) == {
        'message': 'Hermes: Hello back.'}
    assert adapter.handle_message.call_args.args[0].text == 'right'


async def test_duplicate_segments_ignored_within_and_between_calls(server):
    adapter, client = server
    reply_with(adapter)
    question = segment('Hey Hermes, tell me the time')
    assert await post(client, question, question) == {'message': 'Hermes: Hello back.'}
    assert await post(client, question) == {}
    assert adapter.handle_message.await_count == 1
    # The key includes both start and text, and is scoped to the session.
    await post(client, {**question, 'text': 'Hey Hermes, different'})
    await post(client, {**question, 'start': 1})
    await post(client, question, url=URL.replace('live', 'other'))
    assert adapter.handle_message.await_count == 4


async def test_buffer_and_seen_segments_are_isolated_by_device_identity(server):
    adapter, client = server
    adapter._tokens = parse_devices('alice:ray=tok-alice-1,alice:omi=tok-alice-2,bob:omi=tok-bob-1', '')
    reply_with(adapter)
    assert await post(client, segment('Hermes')) == {}
    for token in ('tok-alice-2', 'tok-bob-1'):
        assert await post(client, segment('private continuation', 1), url=URL.replace('tok-alice-1', token)) == {}
    assert await post(client, segment('our question', 1)) == {'message': 'Hermes: Hello back.'}
    await post(client, segment('Hey Hermes, hello', 2))
    await post(client, segment('Hey Hermes, hello', 2), url=URL.replace('tok-alice-1', 'tok-bob-1'))
    assert adapter.handle_message.await_count == 3
    assert adapter.handle_message.call_args.args[0].source.chat_id == 'bob'


async def test_slow_reply_arrives_on_next_call(server, monkeypatch):
    adapter, client = server
    api_for(adapter).omi_wait = 0.01
    resume, finished = asyncio.Event(), asyncio.Event()

    async def query(conn, frame):
        await resume.wait()
        await adapter._deliver_reply('alice', conn, frame['id'], 'The late answer.')
        finished.set()
    monkeypatch.setattr(adapter, '_process_query', query)
    assert await post(client, segment('Hey Hermes, slow question')) == {}
    assert not adapter.registry.live('alice')
    resume.set()
    await asyncio.wait_for(finished.wait(), 2)
    assert await post(client) == {'message': 'Hermes: The late answer.'}
    assert await post(client) == {}
    assert not adapter.outbox._inflight


async def test_queued_pushes_and_reply_join_oldest_first_and_isolate_user(server):
    adapter, client = server
    await adapter.send('bob', 'Private reminder.')
    await adapter.send('alice', 'First reminder.')
    await adapter.send('alice', 'Second reminder.', metadata={'origin': 'cron'})
    reply_with(adapter, 'The answer.')
    assert await post(client, segment('Hey Hermes, hello')) == {
        'message': 'Hermes: First reminder.\n\nSecond reminder.\n\nThe answer.'}
    assert await post(client) == {}
    assert not adapter.outbox._inflight
    assert [item[1] for item in adapter.outbox.drain('bob')] == ['Private reminder.']


async def test_pushes_remain_tracked_until_answer_is_written(server, monkeypatch):
    adapter, client = server
    await adapter.send('alice', 'Remember to stretch.')
    prepare = http_api.web.Response.prepare
    pending = []

    async def watched(response, request):
        pending.extend(item[1][1] for item in adapter.outbox._inflight.values())
        return await prepare(response, request)
    monkeypatch.setattr(http_api.web.Response, 'prepare', watched)
    assert await post(client) == {'message': 'Hermes: Remember to stretch.'}
    assert pending and all(text == 'Remember to stretch.' for text in pending)
    assert adapter.outbox.drain('alice') == [] and not adapter.outbox._inflight


async def test_failed_answer_retains_push_and_reply(server, monkeypatch):
    adapter, client = server
    await adapter.send('alice', 'Remember to stretch.')
    reply_with(adapter, 'An answer to keep.')

    async def broken(response, request):
        raise ConnectionResetError('gone')
    with monkeypatch.context() as patch:
        patch.setattr(http_api.web.Response, 'prepare', broken)
        with pytest.raises(Exception):
            await client.post(URL, json=[segment('Hey Hermes, hello')])
    assert not adapter.registry.live('alice')
    assert await post(client) == {'message': 'Hermes: Remember to stretch.\n\nAn answer to keep.'}
    assert not adapter.outbox._inflight


async def test_message_trimming_keeps_whole_items_and_queues_the_rest(server):
    adapter, client = server
    oldest, middle, newest = 'A' * 500, 'B' * 600, 'C' * 300
    for text in (oldest, middle, newest):
        await adapter.send('alice', text)
    assert await post(client) == {'message': 'Hermes: ' + oldest}  # the middle does not fit: it and the rest wait
    assert await post(client) == {'message': 'Hermes: ' + middle + '\n\n' + newest}
    assert await post(client) == {}


async def test_in_time_reply_after_a_long_push_is_queued_whole(server):
    adapter, client = server
    await adapter.send('alice', 'P' * 900)
    reply_with(adapter, 'R' * 300)
    assert await post(client, segment('Hermes, answer me')) == {'message': 'Hermes: ' + 'P' * 900}
    assert await post(client) == {'message': 'Hermes: ' + 'R' * 300}
    assert await post(client) == {}


async def test_oversized_push_is_shown_once_with_its_head(server):
    adapter, client = server
    await adapter.send('alice', 'prefix' + 'x' * 1000)
    await adapter.send('alice', 'Next reminder.')
    assert await post(client) == {'message': 'Hermes: prefix' + 'x' * 985 + '…'}
    assert await post(client) == {'message': 'Hermes: Next reminder.'}
    assert await post(client) == {}


async def test_in_time_reply_keeps_its_head(server):
    adapter, client = server
    reply_with(adapter, 'Answer starts here: ' + 'x' * 1000)
    result = await post(client, segment('Hermes, long answer'))
    assert result == {'message': 'Hermes: ' + ('Answer starts here: ' + 'x' * 1000)[:991] + '…'}
    assert await post(client) == {}


async def test_reply_without_room_is_queued_for_next_call(server):
    adapter, client = server
    await adapter.send('alice', 'x' * 1000)
    reply_with(adapter, 'Keep my answer.')
    assert await post(client, segment('Hermes, hello')) == {'message': 'Hermes: ' + 'x' * 991 + '…'}
    assert await post(client) == {'message': 'Hermes: Keep my answer.'}
    assert await post(client) == {}


async def test_short_reply_is_long_enough_for_omi_notification(server):
    adapter, client = server
    reply_with(adapter, '4.')
    assert await post(client, segment('Hermes, two plus two')) == {'message': 'Hermes: 4.'}


async def test_idle_session_expires_on_next_call_and_active_session_survives(server, monkeypatch):
    from glasses_plugin import omi
    adapter, client = server
    now = [10.0]
    monkeypatch.setattr(omi, 'monotonic', lambda: now[0])
    reply_with(adapter)
    question = segment('Hermes, hello')
    await post(client, question)
    now[0] += 599
    await post(client, url=URL.replace('live', 'active'))
    now[0] += 2
    await post(client, url=URL.replace('live', 'active'))
    assert [key[1] for key in api_for(adapter).sessions] == ['active']
    assert await post(client, question) == {'message': 'Hermes: Hello back.'}


async def test_seen_keys_drop_oldest_beyond_256(server):
    adapter, client = server
    reply_with(adapter)
    question = segment('Hermes, hello')
    await post(client, question)
    await post(client, *(segment('background', i) for i in range(1, 257)))
    state = next(iter(api_for(adapter).sessions.values()))
    assert len(state['seen']) == 256
    await post(client, segment('background', 1))  # duplicate must not refresh oldest key
    await post(client, question)
    assert adapter.handle_message.await_count == 2
    assert (1, 'background') not in state['seen']


async def test_sessions_are_capped_per_identity_and_evict_least_recently_used(server):
    adapter, client = server
    adapter._tokens = parse_devices('alice:ray=tok-alice-1,bob:omi=tok-bob-1', '')
    await post(client, url=URL.replace('tok-alice-1', 'tok-bob-1'))
    for i in range(32):
        await post(client, url=URL.replace('live', str(i)))
    await post(client, url=URL.replace('live', '0'))
    await post(client, url=URL.replace('live', '32'))
    keys = list(api_for(adapter).sessions)
    alice = [session for identity, session in keys if identity.user == 'alice']
    assert len(alice) == 32 and '0' in alice and '32' in alice and '1' not in alice
    assert any(identity.user == 'bob' for identity, _ in keys)


@pytest.mark.parametrize('raw', ['{}', 'null', '1', '"text"', '[1]', '[{"text": 2}]', '{'])
async def test_malformed_body_is_rejected_without_consuming_pushes(server, raw):
    adapter, client = server
    await adapter.send('alice', 'Keep this reminder.')
    assert (await client.post(URL, data=raw)).status == 400
    assert [item[1] for item in adapter.outbox.drain('alice')] == ['Keep this reminder.']


async def test_json_recursion_error_is_rejected_without_consuming_pushes(server, monkeypatch):
    adapter, client = server
    await adapter.send('alice', 'Keep this reminder.')
    # Decoder recursion behaviour varies by Python version; force its documented failure.
    monkeypatch.setattr(http_api.web.Request, 'json', AsyncMock(side_effect=RecursionError('nested JSON')))
    response = await client.post(URL, data='[' * 2000 + '0' + ']' * 2000)
    assert response.status == 400
    assert await response.json() == {'error': 'malformed JSON'}
    assert [item[1] for item in adapter.outbox.drain('alice')] == ['Keep this reminder.']


async def test_omi_wait_setting(make_adapter, monkeypatch):
    from glasses_plugin.omi import OmiApi
    assert OmiApi(make_adapter()).omi_wait == 20
    assert OmiApi(make_adapter(omi_wait=0.125)).omi_wait == 0.125
    monkeypatch.setenv('GLASSES_OMI_WAIT', '0.25')
    assert OmiApi(make_adapter()).omi_wait == 0.25


async def test_multiple_wakes_in_one_call_each_start_a_question(server, monkeypatch):
    adapter, client = server
    asked = []

    async def query(conn, frame):
        asked.append(frame['text'])
        await adapter._deliver_reply('alice', conn, frame['id'], 'Answer: ' + frame['text'])
    monkeypatch.setattr(adapter, '_process_query', query)
    assert await post(client, segment('Hey Hermes, first question'), segment('Hermes, second question', 1)) == {
        'message': 'Hermes: Answer: first question\n\nAnswer: second question'}
    assert asked == ['first question', 'second question']


async def test_wake_and_continuation_in_one_call(server):
    adapter, client = server
    reply_with(adapter)
    assert await post(client, segment('Hey Hermes'), segment('tell me', 1), segment('the time', 2)) == {
        'message': 'Hermes: Hello back.'}
    assert adapter.handle_message.call_args.args[0].text == 'tell me the time'


@pytest.mark.parametrize('position', ['before_close', 'during_close'])
async def test_push_at_finish_is_buffered_or_queued(server, monkeypatch, position):
    from glasses_plugin import omi
    adapter, client = server
    tasks = []
    if position == 'before_close':
        finish = omi._finish

        async def boundary(adapter, conn):
            await adapter.send('alice', 'Boundary reminder.')
            return await finish(adapter, conn)
        monkeypatch.setattr(omi, '_finish', boundary)
    else:
        close = http_api.HttpSocket.close

        async def boundary(socket, code=1000, message=b''):
            await close(socket, code, message)
            tasks.append(asyncio.create_task(adapter.send('alice', 'Boundary reminder.')))
            await asyncio.sleep(0)
            assert not tasks[-1].done()
        monkeypatch.setattr(http_api.HttpSocket, 'close', boundary)
    result = await post(client)
    await asyncio.gather(*tasks)
    queued = adapter.outbox.drain('alice')
    assert result == ({'message': 'Hermes: Boundary reminder.'} if position == 'before_close' else {})
    assert [item[1] for item in queued] == ([] if position == 'before_close' else ['Boundary reminder.'])


async def test_websocket_is_not_replaced_and_live_push_is_in_answer(server, monkeypatch):
    from glasses_plugin.devices import Connection, Identity
    adapter, client = server
    websocket = Connection(http_api.HttpSocket(), Identity('alice', 'ray'), protocol=1)
    websocket.flushed = True
    await adapter.registry.add(websocket)

    async def query(conn, frame):
        assert not websocket.ws.closed
        assert conn.slot and conn is not websocket
        await adapter.send('alice', 'Live reminder.', metadata={'origin': 'cron'})
        await adapter._deliver_reply('alice', conn, frame['id'], 'Live answer.')
    monkeypatch.setattr(adapter, '_process_query', query)
    assert await post(client, segment('Hey Hermes, hello')) == {'message': 'Hermes: Live reminder.\n\nLive answer.'}
    assert adapter.registry.live('alice') == [websocket]
    assert not websocket.ws.closed


async def test_body_size_limit_is_preserved(server):
    adapter, client = server
    adapter._app._client_max_size = 64
    assert (await client.post(URL, json=[segment('x' * 100)])).status == 413


async def test_unexpected_answer_error_still_releases_and_requeues(server, monkeypatch):
    adapter, client = server
    await adapter.send('alice', 'Keep this reminder.')
    reply_with(adapter, 'Keep this answer.')

    def broken(*args):
        raise KeyError('answer failed')
    monkeypatch.setattr(api_for(adapter), '_answer', broken)
    response = await client.post(URL, json=[segment('Hey Hermes, hello')])
    assert response.status == 500
    assert not adapter.registry.live('alice')
    assert [item[1] for item in adapter.outbox.drain('alice')] == ['Keep this reminder.', 'Keep this answer.']


@pytest.mark.parametrize('words,choice', [('yes', 'once'), ('no', 'deny')])
async def test_spoken_approval_uses_existing_path(server, monkeypatch, words, choice):
    from glasses_plugin.tests.test_spoken_approval import ask_approval
    adapter, client = server
    adapter.handle_message = AsyncMock()
    queue, calls = [], []

    def resolve(key, value, resolve_all=False, reason=None, request_id=None):
        calls.append((key, value, request_id))
        queue.remove(request_id)
        return 1
    resolve.queue = queue
    monkeypatch.setattr('tools.approval.resolve_gateway_approval', resolve)
    monkeypatch.setattr('tools.approval.list_gateway_approvals', lambda key: [{'request_id': i} for i in queue])
    monkeypatch.setattr('tools.clarify_gateway.has_pending', lambda key: False)
    await ask_approval(adapter, resolve)
    prompt = await post(client)
    assert 'Say yes to allow it once' in prompt['message']
    answer = await post(client, segment('Hey Hermes, ' + words, is_user=True))
    assert 'message' in answer and len(answer['message']) > 5
    assert calls == [('agent:main:glasses:dm:alice', choice, 'r1')]
    adapter.handle_message.assert_not_called()


@pytest.mark.parametrize('marks', [{}, {'is_user': False}])
async def test_only_the_wearers_marked_speech_answers_an_approval(server, monkeypatch, marks):
    from glasses_plugin.tests.test_spoken_approval import ask_approval
    adapter, client = server
    adapter.handle_message = AsyncMock()
    queue, calls = [], []

    def resolve(key, value, resolve_all=False, reason=None, request_id=None):
        calls.append((key, value, request_id))
        return 1
    resolve.queue = queue
    monkeypatch.setattr('tools.approval.resolve_gateway_approval', resolve)
    monkeypatch.setattr('tools.approval.list_gateway_approvals', lambda key: [{'request_id': i} for i in queue])
    monkeypatch.setattr('tools.clarify_gateway.has_pending', lambda key: False)
    await ask_approval(adapter, resolve)
    await post(client)
    # someone nearby (or speech with no speaker marks) says yes: nothing resolves and Hermes never hears the bare yes
    answer = await post(client, segment('Hey Hermes, yes', **marks))
    assert answer['message'].endswith('Only your own voice can answer an approval. Use /approve or /deny.')
    assert calls == [] and queue == ['r1']
    adapter.handle_message.assert_not_called()


async def test_two_omitted_replies_stay_separate_items(server, monkeypatch):
    adapter, client = server
    await adapter.send('alice', 'P' * 900)

    async def query(conn, frame):
        await conn.send_json({'type': 'response', 'turn': frame['id'], 'text': frame['text'][:1] * 600, 'final': True})
    monkeypatch.setattr(adapter, '_process_query', query)
    assert await post(client, segment('Hermes, aaa', 1), segment('Hermes, bbb', 30)) == {'message': 'Hermes: ' + 'P' * 900}
    second = await post(client)
    third = await post(client)
    assert sorted([second['message'], third['message']]) == ['Hermes: ' + 'a' * 600, 'Hermes: ' + 'b' * 600]
    assert await post(client) == {}


async def test_failed_write_queues_each_reply_once(server, monkeypatch):
    adapter, client = server
    await adapter.send('alice', 'P' * 900)
    reply_with(adapter, 'R' * 300)

    async def broken(self, request):
        raise ConnectionResetError('client went away')
    monkeypatch.setattr(http_api.web.Response, 'prepare', broken)
    try:
        await post(client, segment('Hermes, answer me'))
    except Exception:
        pass
    monkeypatch.undo()
    texts = [item[1] for item in adapter.outbox.drain('alice')]
    assert sorted(texts) == ['P' * 900, 'R' * 300]


async def test_idle_session_is_dropped_by_timer_without_another_call(server, monkeypatch):
    from glasses_plugin import omi
    adapter, client = server
    monkeypatch.setattr(omi, 'SESSION_SECONDS', 0.5)
    reply_with(adapter)
    await post(client, segment('Hermes, hello'))
    api = api_for(adapter)
    assert len(api.sessions) == 1
    await asyncio.sleep(1.5)  # no further webhook call arrives
    assert api.sessions == {} and api._timer is None


async def test_timer_rearms_for_a_session_still_active(server, monkeypatch):
    from glasses_plugin import omi
    adapter, client = server
    monkeypatch.setattr(omi, 'SESSION_SECONDS', 1.0)
    await post(client)
    await asyncio.sleep(0.6)
    await post(client, url=URL.replace('live', 'later'))
    api = api_for(adapter)
    await asyncio.sleep(0.7)  # the first session is idle past the window; the second is not
    assert [key[1] for key in api.sessions] == ['later'] and api._timer is not None
    await asyncio.sleep(0.9)
    assert api.sessions == {} and api._timer is None
