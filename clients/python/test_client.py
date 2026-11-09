"""Protocol behavior tests: localhost peers only, with deterministic deadline control."""
import asyncio
import importlib
import importlib.util
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

# Works both standalone and with tests/conftest.py's Hermes import isolation.
path = Path(__file__).with_name('hermes_glasses.py')
spec = importlib.util.spec_from_file_location('hermes_glasses', path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
Client, Http, Error = module.HermesGlassesClient, module.HermesGlassesHttp, module.HermesGlassesError


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


class Peer:
    def __init__(self, features=()):
        self.frames = asyncio.Queue()
        self.requests, self.sockets = [], []
        self.features = list(features)
        self.received = []

    async def handler(self, request):
        self.requests.append((request.path_qs, request.headers.get('Authorization')))
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.append(ws)
        await ws.send_json({'type': 'welcome', 'protocol_max': 1})
        async for msg in ws:
            frame = msg.json()
            await self.frames.put(frame)
            if frame['type'] == 'hello':
                await ws.send_json({'type': 'hello_ack', 'protocol': 1, 'user': 'fixture',
                                    'device': 'test', 'server': {'features': self.features, 'version': 'fixture'}})
        return ws

    async def next(self, kind):
        async with asyncio.timeout(2):
            while True:
                frame = await self.frames.get()
                if frame['type'] == kind:
                    return frame

    async def send(self, frame):
        before = len(self.received)
        await self.sockets[-1].send_json(frame)
        await until(lambda: len(self.received) > before)

    async def ask(self, **kwargs):
        task = asyncio.create_task(self.client.ask(**kwargs))
        return task, await self.next('query')


@asynccontextmanager
async def connected(*, caps=None, features=(), reconnect=False, **kwargs):
    peer = Peer(features)
    app = web.Application()
    app.router.add_get('/voice', peer.handler)
    async with TestServer(app) as server:
        client = peer.client = Client(str(server.make_url('/voice?extra=1')).replace('http:', 'ws:'),
                                     'fixture-secret', {'name': 'test', 'capabilities': caps or {}},
                                     reconnect=reconnect, **kwargs)
        receive = client._receive

        def observed(frame):
            receive(frame)
            peer.received.append(frame)

        client._receive = observed
        try:
            hello, same = await asyncio.gather(client.connect(), client.connect())
            assert hello == same == await client.connect()
            yield peer
        finally:
            await client.disconnect()


class Clock:
    """Capture only client deadlines; aiohttp/test-server timeouts stay real."""
    def __init__(self, monkeypatch):
        self.calls = []
        loop = asyncio.get_running_loop()
        original = loop.call_later

        def later(delay, fn, *args, **kwargs):
            owner = getattr(fn, '__self__', None)
            if isinstance(owner, Client) or getattr(fn, '__module__', '') == module.__name__:
                handle = asyncio.Handle(fn, args, loop)
                self.calls.append((delay, handle))
                return handle
            return original(delay, fn, *args, **kwargs)

        monkeypatch.setattr(loop, 'call_later', later)

    def fire(self, delay):
        for seconds, handle in self.calls:
            if seconds == delay and not handle.cancelled():
                handle._run()
                handle.cancel()
                return
        raise AssertionError(f'No live timer at {delay}')


@pytest.mark.asyncio
async def test_conformance_scenario_all_checks_pass():
    kit = importlib.import_module('glasses_plugin.conformance' if 'glasses_plugin' in sys.modules else 'conformance')
    report_done = asyncio.get_running_loop().create_future()
    app = kit.fake_app('fixture', on_report=report_done.set_result)
    async with TestServer(app) as server:
        client = Client(str(server.make_url('/voice')).replace('http:', 'ws:'), 'fixture',
                        {'name': 'python', 'capabilities': {'ack': True, 'camera': True, 'buttons': True,
                         'display': True, 'haptics': True, 'audio_out': ['audio/wav']}}, reconnect=False)
        client.on_capture_photo = lambda reason: '/9j/2Q=='
        events, approvals = [], []
        for event in ('push', 'display', 'haptic', 'delta', 'transcript', 'audio', 'approval_closed'):
            client.on(event, lambda frame, event=event: events.append((event, frame)))

        async def approve(frame):
            approvals.append(await client.answer_approval(frame['id'], 'once'))

        client.on('approval_request', approve)
        try:
            assert (await client.connect())['protocol'] == 1
            await client.new_session()
            result = await client.ask(id='voice', audio={'data': 'AA==', 'mime': 'audio/wav'})
            assert (result.id, result.text, result.transcript) == ('voice', 'Fixture reply', 'Fixture transcript')
            assert result.audio['mime'] == 'audio/wav'
            report = await asyncio.wait_for(report_done, 2)
            assert {item['check']: item['status'] for item in report.checks} == {
                key: 'PASS' for key in ('hello', 'ack', 'photo', 'approval_response', 'query')}
            await until(lambda: approvals)
            assert approvals[0]['ok'] is True
            assert {name for name, _ in events} == {'push', 'display', 'haptic', 'delta', 'transcript', 'audio', 'approval_closed'}
        finally:
            await client.disconnect()


@pytest.mark.asyncio
async def test_handshake_bearer_media_correlation_deltas_status_and_listeners():
    async with connected(app={'name': 'test', 'version': '1'}) as peer:
        client = peer.client
        hello = await peer.next('hello')
        assert hello == {'type': 'hello', 'protocol': 1, 'device': client.device, 'app': client.app}
        assert peer.requests == [('/voice?extra=1', 'Bearer fixture-secret')]
        seen, status, transcript = [], [], []
        off = client.on('delta', seen.append)
        client.on('status', status.append)
        client.on('transcript', transcript.append)
        await peer.send({'type': 'status', 'state': 'idle'})
        assert status[-1]['id'] is None
        one, frame = await peer.ask(text='one', id='one', image={'data': 'photo', 'mime': 'image/jpeg'})
        assert frame['image']['data'] == 'photo'
        await peer.send({'type': 'status', 'state': 'thinking'})
        assert status[-1]['id'] == 'one'
        two, frame = await peer.ask(id='two', audio={'data': 'voice', 'mime': 'audio/wav'}, speak=False)
        assert frame['audio']['data'] == 'voice' and frame['speak'] is False
        await peer.send({'type': 'status', 'state': 'working'})
        assert status[-1]['id'] is None
        await peer.send({'type': 'status', 'turn': 'two', 'state': 'working'})
        assert status[-1]['id'] == 'two'
        for text in ('a', 'answer'):
            await peer.send({'type': 'response_delta', 'turn': 'two', 'text': text, 'future': True})
        assert [f['text'] for f in seen] == ['a', 'answer']
        assert seen[-1]['id'] == 'two' and seen[-1]['future'] is True
        off()
        await peer.send({'type': 'response_delta', 'turn': 'two', 'text': 'ignored'})
        assert len(seen) == 2
        client.off('status', status.append)
        await peer.send({'type': 'transcript', 'turn': 'two', 'text': 'spoken'})
        assert transcript[-1]['id'] == 'two'
        await peer.send({'type': 'future', 'extra': 1})
        for raw in ('not json', '[]', 'null'):
            await peer.sockets[-1].send_str(raw)
        await peer.send({'type': 'response', 'turn': 'stray', 'text': 'wrong'})
        await peer.send({'type': 'response', 'turn': 'two', 'text': 'second'})
        assert (await two).transcript == 'spoken' and not one.done()
        await peer.send({'type': 'response', 'turn': 'one', 'text': 'first'})
        assert await one == module.TurnResult('one', 'first')


@pytest.mark.asyncio
@pytest.mark.parametrize('correlation', ['turn', 'id', None])
async def test_errors_correlate_and_preserve_text_and_reset(correlation):
    async with connected(caps={'audio_out': ['audio/wav']}, features=['audio_out']) as peer:
        a, _ = await peer.ask(id='a')
        b, _ = await peer.ask(id='b')
        c, _ = await peer.ask(id='c')
        await peer.send({'type': 'response', 'turn': 'a', 'text': 'kept'})
        reset = asyncio.create_task(peer.client.new_session())
        await peer.next('new_session')
        await peer.send({'type': 'error', 'message': 'failed fixture-secret', **({correlation: 'b'} if correlation else {})})
        with pytest.raises(Error) as failure:
            await b
        assert failure.value.code == 'server' and 'fixture-secret' not in str(failure.value)
        if correlation:
            assert not a.done() and not c.done()
            await peer.send({'type': 'error', correlation: 'a', 'message': 'speech failed'})
            await peer.send({'type': 'response', 'turn': 'c', 'text': 'third'})
            await peer.send({'type': 'audio', 'ref': 'c', 'data': 'sound', 'mime': 'audio/wav'})
            assert (await c).audio['data'] == 'sound'
        else:
            with pytest.raises(Error):
                await c
        assert (await a).text == 'kept'
        assert not reset.done()
        await peer.send({'type': 'session_reset'})
        assert (await reset)['type'] == 'session_reset'


@pytest.mark.asyncio
@pytest.mark.parametrize('message,query', [('reset failed', False), ('New session failed.', True)])
async def test_reset_coalesces_and_rejects_own_errors(message, query):
    async with connected() as peer:
        client = peer.client
        a = asyncio.create_task(client.new_session())
        b = asyncio.create_task(client.new_session())
        await peer.next('new_session')
        await asyncio.sleep(0)
        assert peer.frames.empty()
        if query:
            ask, _ = await peer.ask()
        await peer.send({'type': 'error', 'message': message})
        for task in (a, b, *([ask] if query else [])):
            with pytest.raises(Error, match=message):
                await task
        assert client._reset is None


@pytest.mark.asyncio
@pytest.mark.parametrize('declared,advertised,speak,wait', [
    (True, True, None, True), (False, True, None, False),
    (True, False, None, False), (True, True, False, False)])
async def test_audio_wait_is_15_seconds_only_when_negotiated(monkeypatch, declared, advertised, speak, wait):
    async with connected(caps={'audio_out': ['audio/wav']} if declared else {},
                         features=['audio_out'] if advertised else []) as peer:
        clock = Clock(monkeypatch)
        query, _ = await peer.ask(id='q', speak=speak)
        await peer.send({'type': 'response', 'turn': 'q', 'text': 'text'})
        if wait:
            assert not query.done()
            assert [seconds for seconds, _ in clock.calls] == [180, 15]
            clock.fire(15)
        assert (await query).text == 'text'
        assert all(handle.cancelled() for _, handle in clock.calls)


@pytest.mark.asyncio
async def test_audio_ref_duplicate_response_and_push_audio():
    async with connected(caps={'audio_out': ['audio/wav']}, features=['audio_out']) as peer:
        audio = []
        peer.client.on('audio', audio.append)
        query, _ = await peer.ask(id='q')
        await peer.send({'type': 'audio', 'ref': 'q', 'data': 'early', 'mime': 'audio/wav'})
        assert not query.done()
        await peer.send({'type': 'response', 'turn': 'q', 'text': 'first'})
        await peer.send({'type': 'response', 'turn': 'q', 'text': 'duplicate'})
        await peer.send({'type': 'audio', 'ref': 'push', 'data': 'push', 'mime': 'audio/wav'})
        assert not query.done()
        await peer.send({'type': 'audio', 'ref': 'q', 'data': 'reply', 'mime': 'audio/wav'})
        assert (await query).text == 'first'
        assert (await query).audio == {'data': 'reply', 'mime': 'audio/wav'}
        assert [f['data'] for f in audio] == ['early', 'push', 'reply']


@pytest.mark.asyncio
async def test_deadline_zero_cancellation_unique_ids_and_local_wait_only(monkeypatch):
    async with connected() as peer:
        clock = Clock(monkeypatch)
        timed, first = await peer.ask(id='folded')
        unlimited, second = await peer.ask(timeout=0)
        assert first['id'] != second['id']
        assert clock.calls[0][0] == 180 and len(clock.calls) == 1
        clock.fire(180)
        with pytest.raises(Error) as failure:
            await timed
        assert failure.value.code == 'timeout'
        assert not unlimited.done()
        for bad in ('', 123, second['id']):
            with pytest.raises(Error, match='unique'):
                await peer.client.ask(id=bad)
        unlimited.cancel()
        with pytest.raises(Error) as failure:
            await unlimited
        assert failure.value.code == 'aborted'
        assert not peer.client._queries and peer.frames.empty()
        await peer.send({'type': 'response', 'turn': second['id'], 'text': 'too late'})
        custom, frame = await peer.ask(timeout=0.25)
        clock.fire(0.25)
        with pytest.raises(Error) as failure:
            await custom
        assert failure.value.code == 'timeout'
        assert peer.client.ready and peer.frames.empty()
        other = Client('ws://localhost/voice', 'fixture', {'name': 'other'})
        assert other._next_id() != peer.client._next_id()


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit', [False, True])
async def test_interrupt_fresh_stop_reply_consumed_and_oldest_non_slash(explicit):
    async with connected() as peer:
        slash, _ = await peer.ask(id='slash', text='/status')
        one, _ = await peer.ask(id='one', text='hello')
        two, _ = await peer.ask(id='two', text='later')
        peer.client.interrupt(ask='two' if explicit else None)
        stop = await peer.next('interrupt')
        assert stop['id'] not in ('slash', 'one', 'two')
        with pytest.raises(module.Interrupted) as failure:
            await (two if explicit else one)
        assert failure.value.code == 'interrupted'
        with pytest.raises(Error, match='unique'):
            await peer.client.ask(id=stop['id'])
        await peer.send({'type': 'response', 'turn': stop['id'], 'text': 'Stopped.'})
        assert stop['id'] not in peer.client._interrupts
        survivor = one if explicit else two
        assert not survivor.done() and not slash.done()
        await peer.send({'type': 'response', 'turn': 'one' if explicit else 'two', 'text': 'own'})
        await peer.send({'type': 'response', 'turn': 'slash', 'text': 'status'})
        assert (await survivor).text == 'own' and (await slash).text == 'status'


@pytest.mark.asyncio
async def test_interrupt_keeps_text_waiting_for_audio():
    async with connected(caps={'audio_out': ['audio/wav']}, features=['audio_out']) as peer:
        query, _ = await peer.ask(id='q')
        await peer.send({'type': 'response', 'turn': 'q', 'text': 'kept'})
        peer.client.interrupt('q')
        await peer.next('interrupt')
        assert (await query).text == 'kept'


@pytest.mark.asyncio
@pytest.mark.parametrize('ack', [False, True])
async def test_ack_only_when_declared_and_manual_photos(ack):
    async with connected(caps={'ack': ack}) as peer:
        pushes = []
        peer.client.on('push', pushes.append)
        await peer.send({'type': 'push', 'id': 'push', 'text': 'hi'})
        peer.client.send_photo('camera', '/9j/2Q==')
        photo = await peer.next('ack' if ack else 'photo')
        assert photo['type'] == ('ack' if ack else 'photo')
        if ack:
            assert photo['id'] == 'push'
            photo = await peer.next('photo')
        assert photo == {'type': 'photo', 'request': 'camera', 'data': '/9j/2Q==', 'mime': 'image/jpeg'}
        peer.client.photo_error('camera', 'Unavailable')
        assert (await peer.next('photo_error'))['message'] == 'Unavailable'
        assert pushes[0]['id'] == 'push'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['sync', 'async', 'raise', 'async_raise', 'invalid'])
async def test_camera_sync_async_failure_and_throwing_listener(mode):
    async with connected() as peer:
        order = []

        def camera(reason):
            order.append('camera')
            assert reason == 'tool'
            if mode == 'raise':
                raise ValueError('fixture-secret private camera')
            return 123 if mode == 'invalid' else '/9j/2Q=='

        async def async_camera(reason):
            result = camera(reason)
            if mode == 'async_raise':
                raise ValueError('fixture-secret private camera')
            return result

        def broken_listener(frame):
            order.append('event')
            raise RuntimeError('listener failed')

        peer.client.on_capture_photo = async_camera if mode.startswith('async') else camera
        peer.client.on('capture_photo', broken_listener)
        await peer.send({'type': 'capture_photo', 'request': 'photo', 'reason': 'tool'})
        failure = mode in ('raise', 'async_raise', 'invalid')
        answer = await peer.next('photo_error' if failure else 'photo')
        assert answer['request'] == 'photo'
        if failure:
            assert answer['message'] == 'Camera unavailable.'
        if not mode.startswith('async'):
            assert order == ['camera', 'event']


@pytest.mark.asyncio
async def test_approvals_false_duplicate_and_closed_event():
    async with connected() as peer:
        seen = []
        peer.client.on('approval_closed', seen.append)
        answer = asyncio.create_task(peer.client.answer_approval('a', 'deny'))
        assert await peer.next('approval_response') == {'type': 'approval_response', 'id': 'a', 'choice': 'deny'}
        with pytest.raises(Error, match='already pending'):
            await peer.client.answer_approval('a', 'once')
        await peer.send({'type': 'approval_result', 'id': 'a', 'ok': False, 'message': 'expired'})
        assert (await answer)['ok'] is False
        await peer.send({'type': 'approval_closed', 'id': 'a'})
        assert seen == [{'type': 'approval_closed', 'id': 'a'}]


@pytest.mark.asyncio
async def test_disconnect_fails_every_pending_operation_no_replay_and_fresh_connect(monkeypatch):
    async with connected(reconnect=True) as peer:
        clock = Clock(monkeypatch)
        query, _ = await peer.ask()
        reset = asyncio.create_task(peer.client.new_session())
        approval = asyncio.create_task(peer.client.answer_approval('a', 'once'))
        await peer.next('new_session')
        await peer.next('approval_response')
        await peer.sockets[-1].close(code=1001)
        for task in (query, reset, approval):
            with pytest.raises(Error) as failure:
                await task
            assert failure.value.code == 'disconnected'
        await until(lambda: peer.client._retry is not None)
        delay = next(d for d, h in clock.calls if not h.cancelled())
        assert 0.75 <= delay <= 1
        clock.fire(delay)
        await until(lambda: peer.client.ready)
        await peer.next('hello')
        assert peer.frames.empty() and len(peer.requests) == 2
        await peer.client.disconnect()
        await peer.client.connect()
        await peer.next('hello')
        assert len(peer.requests) == 3
        await peer.client.disconnect()
        assert not peer.client.ready and peer.client._retry is None
        with pytest.raises(Error) as failure:
            await peer.client.ask(text='offline')
        assert failure.value.code == 'disconnected'


@pytest.mark.asyncio
async def test_retry_backoff_jitter_cap_failures_unauthorized_and_disconnect(monkeypatch):
    async with connected(reconnect=True) as peer:
        clock = Clock(monkeypatch)
        client = peer.client
        original = module.aiohttp.ClientSession.ws_connect
        calls = []

        def failed(*args, **kwargs):
            calls.append(1)
            raise aiohttp.ClientConnectionError('fixture-secret transport')

        monkeypatch.setattr(module.aiohttp.ClientSession, 'ws_connect', failed)
        await peer.sockets[-1].close(code=1001)
        expected = [1, 2, 4, 8, 16, 30, 30]
        for index, maximum in enumerate(expected):
            await until(lambda: client._retry is not None)
            delay, handle = next((d, h) for d, h in clock.calls if not h.cancelled())
            assert maximum * 0.75 <= delay <= maximum
            clock.fire(delay)
            await until(lambda: len(calls) == index + 1 and client._runner is None)
        monkeypatch.setattr(module.aiohttp.ClientSession, 'ws_connect', original)
        await client.connect()
        assert client._backoff == 1
        await peer.sockets[-1].close(code=4401)
        await until(lambda: client._runner is None)
        assert client._retry is None
        await client.connect()
        await peer.sockets[-1].close(code=1001)
        await until(lambda: client._runner is None)
        handle = client._retry
        await client.disconnect()
        assert handle.cancelled() and client._retry is None


@pytest.mark.asyncio
@pytest.mark.parametrize('url', ['not a URL fixture-secret', 'ws://[', 'ws://localhost:bad/voice'])
async def test_invalid_url_and_connect_cancellation_are_sanitized(url):
    client = Client(url, 'fixture-secret', {'name': 'test'})
    errors = []
    client.on('error', errors.append)
    with pytest.raises(Error) as failure:
        await client.connect()
    assert failure.value.code == 'disconnected' and 'fixture-secret' not in str(failure.value)
    assert errors == [{'type': 'error', 'message': 'Invalid WebSocket URL.'}]
    assert client._retry is None
    await client.disconnect()
    async with connected() as peer:
        await peer.client.disconnect()
        waiter = asyncio.create_task(peer.client.connect())
        await until(lambda: peer.client._connection is not None)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await until(lambda: peer.client.ready)
        assert (await peer.client.connect())['protocol'] == 1


@pytest.mark.asyncio
async def test_http_bearer_json_pending_needs_answer_plain_errors_and_long_poll():
    calls = []
    status, body = 202, {'status': 'pending'}

    async def handler(request):
        calls.append((request.path_qs, request.method, dict(request.headers),
                      await request.json() if request.method == 'POST' else None))
        if request.headers.get('Accept') == 'text/plain':
            return web.Response(text='plain', status=status)
        return web.json_response(body, status=status)

    app = web.Application()
    app.router.add_route('*', '/{tail:.*}', handler)
    async with TestServer(app) as server:
        http = Http(str(server.make_url('/')), 'fixture-secret')
        assert await http.turn({'text': 'hello'}) == {'status': 'pending'}
        path, method, headers, data = calls[-1]
        assert (path, method, data) == ('/turn', 'POST', {'text': 'hello'})
        assert headers['Authorization'] == 'Bearer fixture-secret'
        assert headers['Accept'] == 'application/json' and headers['Content-Type'] == 'application/json'
        assert await http.turn(text='hello', text_plain=True) == {'status': 'pending', 'text': 'plain'}
        status, body = 200, {'status': 'needs_answer', 'prompts': [{'id': 'a'}]}
        assert await http.turn({'text': 'hello'}) == body
        assert await http.turn({'text': 'hello'}, text_plain=True) == {'status': 'done', 'text': 'plain'}
        body = {'items': []}
        assert await http.pending(wait=0) == body
        assert calls[-1][0:2] == ('/pending?wait=0', 'GET')
        await http.pending()
        assert calls[-1][0] == '/pending?wait=25'
        await http.approval('a', 'deny')
        assert calls[-1][0:2] == ('/approval', 'POST') and calls[-1][3] == {'id': 'a', 'choice': 'deny'}
        status = 401
        with pytest.raises(Error, match='HTTP 401'):
            await http.pending()
    with pytest.raises(Error) as failure:
        await http.pending()
    assert 'fixture-secret' not in str(failure.value)
