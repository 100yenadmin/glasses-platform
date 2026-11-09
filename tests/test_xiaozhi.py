import argparse
import asyncio
import base64
import importlib.util
import json
from pathlib import Path
import struct
import threading
from unittest.mock import AsyncMock

import pytest
from aiohttp import WSMsgType

from glasses_plugin import cli, xiaozhi as xz
from glasses_plugin.adapter import Turn
from glasses_plugin.devices import Connection, Identity
from glasses_plugin.ogg import demux_opus

SPEECH = b's' * 80
QUIET = b'q' * 8


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


async def connect(client, token='tok-alice-1', version=None, header=None, params=None):
    headers = {'Authorization': 'Bearer ' + token}
    if header is not None:
        headers['Protocol-Version'] = str(header)
    ws = await client.ws_connect('/xiaozhi', headers=headers)
    hello = {'type': 'hello'}
    if version is not None:
        hello['version'] = version
    if params is not None:
        hello['audio_params'] = params
    await ws.send_json(hello)
    reply = await ws.receive_json(timeout=1)
    return ws, reply


async def record(ws, packets=(SPEECH,), mode='manual', version=1, stop=True):
    await ws.send_json({'type': 'listen', 'state': 'start', 'mode': mode})
    for packet in packets:
        await ws.send_bytes(xz.binary_packet(packet, version))
    if stop:
        await ws.send_json({'type': 'listen', 'state': 'stop'})


@pytest.mark.asyncio
async def test_ota_auth_fields_time_and_reported_version(server, caplog):
    adapter, client = server
    assert (await client.post('/xiaozhi/ota/no-such-token', json={})).status == 401
    before = int(xz.time.time() * 1000)
    response = await client.post('/xiaozhi/ota/tok-alice-1', json={'application': {'version': '1.2.3'}},
                                 headers={'Host': 'voice.example:8765'})
    body = await response.json()
    assert response.status == 200
    assert body['websocket'] == {'url': 'ws://voice.example:8765/xiaozhi', 'token': 'tok-alice-1', 'version': 1}
    assert body['firmware'] == {'version': '1.2.3', 'url': ''}
    assert before <= body['server_time']['timestamp'] <= xz.time.time() * 1000
    assert body['server_time']['timezone_offset'] == 0
    assert set(body) == {'websocket', 'server_time', 'firmware'}
    assert 'tok-alice-1' not in caplog.text and 'no-such-token' not in caplog.text
    assert (await (await client.post('/xiaozhi/ota/tok-alice-1', json={})).json())['firmware']['version'] == '0'


@pytest.mark.asyncio
async def test_ota_forwarded_proto_and_setting_override(server, monkeypatch):
    adapter, client = server
    async def fetch():
        return await (await client.post('/xiaozhi/ota/tok-alice-1', json={},
                                       headers={'Host': 'proxy.example', 'X-Forwarded-Proto': 'https'})).json()
    assert (await fetch())['websocket']['url'] == 'wss://proxy.example/xiaozhi'
    adapter.config.extra['xiaozhi_url'] = 'wss://configured.example/device'
    assert (await fetch())['websocket']['url'] == 'wss://configured.example/device'
    monkeypatch.setenv('GLASSES_XIAOZHI_URL', 'wss://env.example/xiaozhi')
    # extra_or_secret follows Hermes precedence: an explicit extra value wins.
    adapter.config.extra.pop('xiaozhi_url')
    assert (await fetch())['websocket']['url'] == 'wss://env.example/xiaozhi'


@pytest.mark.asyncio
async def test_ota_invalid_body(server):
    _, client = server
    for body in ([], None, 'bad'):
        assert (await client.post('/xiaozhi/ota/tok-alice-1', json=body)).status == 400


@pytest.mark.asyncio
async def test_bad_websocket_token_and_hello_deadline(server, monkeypatch):
    _, client = server
    ws = await client.ws_connect('/xiaozhi?token=wrong')
    msg = await ws.receive(timeout=1)
    assert msg.type == WSMsgType.CLOSE and msg.data == 4401
    monkeypatch.setattr(xz, 'HELLO_TIMEOUT', 0.01)
    ws = await client.ws_connect('/xiaozhi?token=tok-alice-1')
    msg = await ws.receive(timeout=1)
    assert msg.type == WSMsgType.CLOSE and msg.data == 4400


@pytest.mark.asyncio
@pytest.mark.parametrize('headers', [{}, {'Authorization': 'Bearer '}, {'Authorization': 'Bearer'}])
async def test_missing_or_empty_bearer_token(server, headers):
    adapter, client = server
    ws = await client.ws_connect('/xiaozhi', headers=headers)
    msg = await ws.receive(timeout=1)
    assert msg.type == WSMsgType.CLOSE and msg.data == 4401
    assert not adapter.registry.by_user


@pytest.mark.asyncio
async def test_hello_defaults_capabilities_and_release(server):
    adapter, client = server
    ws = await client.ws_connect('/xiaozhi?token=tok-alice-1')
    await ws.send_json({'type': 'hello'})
    hello = await ws.receive_json(timeout=1)
    assert hello['type'] == 'hello' and hello['transport'] == 'websocket' and hello['session_id']
    assert hello['audio_params'] == {'format': 'opus', 'sample_rate': 24000, 'channels': 1, 'frame_duration': 60}
    conn = adapter.registry.live('alice')[0]
    assert conn.protocol == 1 and conn.ws.version == 1
    assert conn.capabilities == {'audio_out': ['audio/mpeg'], 'speaker': True, 'mic': True,
                                 'display': True, 'ack': True}
    assert conn.flushed
    await ws.close()
    await until(lambda: not adapter.registry.live('alice'))


@pytest.mark.asyncio
@pytest.mark.parametrize('hello', [[], {'type': 'listen'}, {'type': 'hello', 'version': 4},
                                   {'type': 'hello', 'audio_params': {'frame_duration': 0}}])
async def test_invalid_hello_never_registers(server, hello):
    adapter, client = server
    ws = await client.ws_connect('/xiaozhi?token=tok-alice-1')
    await ws.send_json(hello)
    assert (await ws.receive(timeout=1)).data == 4400
    assert adapter.registry.live('alice') == []


@pytest.mark.asyncio
@pytest.mark.parametrize('version,header', [(None, None), (2, None), (3, None), (1, 2), (2, 3)])
async def test_listen_stop_query_and_binary_headers(server, monkeypatch, version, header):
    adapter, client = server
    query = AsyncMock()
    monkeypatch.setattr(adapter, '_process_query', query)
    params = {'sample_rate': 24000, 'channels': 1, 'frame_duration': 20}
    ws, _ = await connect(client, version=version, header=header, params=params)
    actual = header or version or 1
    if actual != 1:
        await ws.send_json({'type': 'listen', 'state': 'start'})
        await ws.send_bytes(b'bad header')
        await ws.send_json({'type': 'listen', 'state': 'stop'})
    await record(ws, (SPEECH, QUIET), version=actual)
    await until(lambda: query.await_count == 1)
    conn, frame = query.call_args.args
    assert frame['type'] == 'query' and frame['text'] == '' and frame['audio']['mime'] == 'audio/ogg'
    assert frame['id'] in conn.asking
    data = base64.b64decode(frame['audio']['data'])
    assert demux_opus(data) == [SPEECH, QUIET]
    assert struct.unpack_from('<I', data, 27 + 1 + 12)[0] == 24000
    assert conn.ws.audio_params == params and conn.ws.version == actual
    await ws.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('version', [1, 2, 3])
async def test_oversized_opus_packet_ignored(server, monkeypatch, version):
    adapter, client = server
    query = AsyncMock()
    monkeypatch.setattr(adapter, '_process_query', query)
    ws, _ = await connect(client, version=version)
    await record(ws, (b'x' * 1501, SPEECH), version=version)
    await until(lambda: query.await_count == 1)
    assert demux_opus(base64.b64decode(query.call_args.args[1]['audio']['data'])) == [SPEECH]
    await ws.close()


@pytest.mark.asyncio
async def test_recording_byte_cap_through_websocket(server, monkeypatch):
    adapter, client = server
    query = AsyncMock()
    monkeypatch.setattr(adapter, '_process_query', query)
    # Short frames ensure the byte cap, rather than the duration cap, ends this recording.
    ws, _ = await connect(client, params={'frame_duration': 2.5})
    await record(ws, [b'x' * 1500] * 161, stop=False)
    await until(lambda: query.await_count == 1)
    packets = demux_opus(base64.b64decode(query.call_args.args[1]['audio']['data']))
    assert len(packets) == 160 and sum(map(len, packets)) == 240000
    await ws.send_json({'type': 'listen', 'state': 'stop'})
    await ws.close()
    await until(lambda: not adapter.registry.by_user)
    assert query.await_count == 1


@pytest.mark.asyncio
async def test_websocket_message_limit(server):
    adapter, client = server
    ws, _ = await connect(client)
    await ws.send_str('x' * (64 * 1024 + 1))
    msg = await ws.receive(timeout=1)
    assert msg.type == WSMsgType.CLOSE and msg.data == 1009
    await until(lambda: not adapter.registry.by_user)


@pytest.mark.asyncio
async def test_disconnect_mid_recording_discards_query_and_registration(server, monkeypatch):
    adapter, client = server
    query = AsyncMock()
    monkeypatch.setattr(adapter, '_process_query', query)
    ws, _ = await connect(client)
    conn = adapter.registry.live('alice')[0]
    await record(ws, stop=False)
    await ws.close()
    await until(lambda: not adapter.registry.by_user)
    assert not query.called and not conn.asking


@pytest.mark.asyncio
async def test_large_recording_mux_runs_off_event_loop(server, monkeypatch):
    adapter, client = server
    query, threads = AsyncMock(), []
    original = xz.mux_opus
    def capture(*args):
        threads.append(threading.get_ident())
        return original(*args)
    monkeypatch.setattr(adapter, '_process_query', query)
    monkeypatch.setattr(xz, 'mux_opus', capture)
    ws, _ = await connect(client)
    await record(ws, [b'x' * 1000] * 40)
    await until(lambda: query.await_count == 1)
    assert threads and all(thread != threading.get_ident() for thread in threads)
    await ws.close()


def test_binary_headers_exact_network_layout_and_validation():
    assert xz.binary_packet(b'abc', 2, 123) == struct.pack('!HHIII', 2, 0, 0, 123, 3) + b'abc'
    assert xz.binary_packet(b'abc', 3) == b'\0\0\0\x03abc'
    for version in (1, 2, 3):
        encoded = xz.binary_packet(SPEECH, version)
        assert xz.strip_binary(encoded, version) == SPEECH
        if version != 1:
            assert xz.strip_binary(encoded[:-1], version) is None
    assert xz.strip_binary(struct.pack('!HHIII', 2, 1, 0, 0, 1) + b'x', 2) is None
    assert xz.strip_binary(b'\1\0\0\1x', 3) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['auto', 'realtime'])
async def test_quiet_packet_end_only_after_speech(server, monkeypatch, mode):
    adapter, client = server
    query = AsyncMock()
    monkeypatch.setattr(adapter, '_process_query', query)
    ws, _ = await connect(client)
    await record(ws, [QUIET] * 14, mode=mode, stop=False)
    await asyncio.sleep(0.01)
    assert query.await_count == 0
    for packet in [SPEECH, *([QUIET] * 14)]:
        await ws.send_bytes(packet)
    await until(lambda: query.await_count == 1)
    assert len(demux_opus(base64.b64decode(query.call_args.args[1]['audio']['data']))) == 29
    await ws.send_json({'type': 'listen', 'state': 'stop'})
    await ws.close()


@pytest.mark.asyncio
async def test_manual_silence_detect_unknown_and_binary_outside_listen_ignored(server, monkeypatch):
    adapter, client = server
    query = AsyncMock()
    monkeypatch.setattr(adapter, '_process_query', query)
    ws, _ = await connect(client)
    await ws.send_bytes(SPEECH)
    for frame in ({'type': 'listen', 'state': 'detect'}, {'type': 'mcp'}, {'type': 'mystery'}, [], 'bad'):
        await ws.send_json(frame)
    await record(ws, [QUIET] * 15)
    await asyncio.sleep(0.01)
    assert query.await_count == 0
    await record(ws, [SPEECH, *([QUIET] * 14)], stop=False)
    await asyncio.sleep(0.01)
    assert query.await_count == 0
    await ws.send_json({'type': 'listen', 'state': 'stop'})
    await until(lambda: query.await_count == 1)
    await ws.close()


@pytest.mark.asyncio
async def test_recording_duration_and_size_caps():
    recording = xz.Recording({})
    assert not any(recording.add(SPEECH) for _ in range(249))
    assert recording.add(SPEECH)
    limited = xz.Recording({}, max_bytes=100)
    assert not limited.add(SPEECH)
    assert limited.add(SPEECH)
    assert demux_opus(base64.b64decode((await limited.query())['audio']['data'])) == [SPEECH]


@pytest.mark.asyncio
async def test_wall_time_recording_end(server, monkeypatch):
    adapter, client = server
    query = AsyncMock()
    monkeypatch.setattr(adapter, '_process_query', query)
    original = xz.Recording
    def short(*args, **kwargs):
        recording = original(*args, **kwargs)
        recording.deadline = xz.time.monotonic() + 0.02
        return recording
    monkeypatch.setattr(xz, 'Recording', short)
    ws, _ = await connect(client)
    await record(ws, stop=False)
    await until(lambda: query.await_count == 1)
    await ws.close()


class Socket:
    def __init__(self):
        self.closed = False
        self.frames = []
    async def send_json(self, frame):
        if self.closed:
            raise ConnectionError()
        self.frames.append(frame)
    async def send_bytes(self, data):
        if self.closed:
            raise ConnectionError()
        self.frames.append(data)
    async def close(self, **kwargs):
        self.closed = True


@pytest.mark.asyncio
async def test_transcript_thinking_display_and_drops():
    real = Socket()
    ws = xz.XiaozhiSocket(real, 'session')
    assert await ws.send_json({'type': 'transcript', 'turn': 'q', 'text': 'heard'})
    for state in ('thinking', 'working', 'thinking'):
        await ws.send_json({'type': 'status', 'state': state})
    await ws.send_json({'type': 'display', 'text': 'shown'})
    before = list(real.frames)
    for kind in ('haptic', 'hello_ack', 'welcome', 'session_reset', 'pong', 'response_delta',
                 'capture_photo', 'approval_request', 'approval_result', 'approval_closed'):
        assert await ws.send_json({'type': kind})
    assert real.frames == before == [
        {'type': 'stt', 'text': 'heard', 'session_id': 'session'},
        {'type': 'llm', 'emotion': 'thinking', 'text': '🤔', 'session_id': 'session'},
        {'type': 'alert', 'status': 'Hermes', 'message': 'shown', 'emotion': 'neutral', 'session_id': 'session'}]
    await ws.send_json({'type': 'status', 'state': 'idle'})
    await ws.send_json({'type': 'status', 'state': 'working'})
    assert len(real.frames) == 4
    await ws.close()
    assert not await ws.send_json({'type': 'display', 'text': 'closed'})


@pytest.mark.asyncio
@pytest.mark.parametrize('version', [1, 2, 3])
async def test_response_push_audio_order_headers_and_pacing(monkeypatch, version):
    real = Socket()
    delays, sleep = [], asyncio.sleep
    async def paced(delay):
        delays.append(delay)
        await sleep(0)
    monkeypatch.setattr(xz.asyncio, 'sleep', paced)
    ws = xz.XiaozhiSocket(real, 's', version, audio_timeout=0.2)
    convert = AsyncMock(return_value=[b'one', b'two'])
    monkeypatch.setattr(xz, 'transcode', convert)
    for frame in ({'type': 'response', 'turn': 'q', 'text': 'answer'},
                  {'type': 'push', 'id': 'p', 'text': 'reminder', 'origin': 'cron'},
                  {'type': 'audio', 'ref': 'p', 'mime': 'audio/mpeg', 'data': 'bXAz'},
                  {'type': 'audio', 'ref': 'other', 'mime': 'audio/mpeg', 'data': 'd3Jvbmc='},
                  {'type': 'audio', 'ref': 'q', 'mime': 'audio/wav', 'data': 'd3Jvbmc='},
                  {'type': 'audio', 'ref': 'q', 'mime': 'audio/mpeg', 'data': 'bXAz'}):
        assert await ws.send_json(frame)
    await asyncio.wait_for(ws.worker, 1)
    expected = []
    for text in ('answer', 'reminder'):
        expected.extend([{'type': 'tts', 'state': 'start', 'session_id': 's'},
                         {'type': 'tts', 'state': 'sentence_start', 'text': text, 'session_id': 's'},
                         xz.binary_packet(b'one', version, 0), xz.binary_packet(b'two', version, 60),
                         {'type': 'tts', 'state': 'stop', 'session_id': 's'}])
    assert real.frames == expected and convert.await_count == 2
    assert delays == [0.055] * 4
    assert [call.args for call in convert.await_args_list] == [(b'mp3',), (b'mp3',)]
    assert ws.waiting == {}
    await ws.close()


@pytest.mark.asyncio
async def test_missing_audio_timeout_error_and_invalid_audio_text_fallback():
    real = Socket()
    ws = xz.XiaozhiSocket(real, 's', audio_timeout=0.01)
    await ws.send_json({'type': 'response', 'turn': 'q', 'text': 'no audio'})
    await ws.send_json({'type': 'error', 'message': 'error text'})
    await ws.send_json({'type': 'push', 'id': 'p', 'text': 'bad audio'})
    await ws.send_json({'type': 'audio', 'ref': 'p', 'mime': 'audio/mpeg', 'data': '%%%bad'})
    await asyncio.wait_for(ws.worker, 1)
    assert [f['state'] for f in real.frames] == ['start', 'sentence_start', 'stop'] * 3
    assert [f['text'] for f in real.frames if 'text' in f] == ['no audio', 'error text', 'bad audio']
    await ws.close()


@pytest.mark.asyncio
async def test_missing_ffmpeg_warns_once_and_text_only(monkeypatch, caplog):
    monkeypatch.setattr(xz, '_missing_ffmpeg', False)
    process = AsyncMock(side_effect=FileNotFoundError())
    monkeypatch.setattr(xz.asyncio, 'create_subprocess_exec', process)
    real = Socket()
    ws = xz.XiaozhiSocket(real, 's')
    for ref in ('q', 'p'):
        await ws.send_json({'type': 'response', 'turn': ref, 'text': ref})
        await ws.send_json({'type': 'audio', 'ref': ref, 'mime': 'audio/mpeg', 'data': 'bXAz'})
    await asyncio.wait_for(ws.worker, 1)
    assert caplog.text.count('ffmpeg unavailable') == 1
    assert all(isinstance(f, dict) for f in real.frames)
    argv = process.call_args.args
    assert argv[0] == 'ffmpeg' and argv[argv.index('-ar') + 1] == '24000'
    assert argv[argv.index('-ac') + 1] == '1' and argv[argv.index('-frame_duration') + 1] == '60'
    assert 'shell' not in process.call_args.kwargs
    await ws.close()


@pytest.mark.asyncio
async def test_abort_stops_playback_and_discards_old_audio(monkeypatch):
    real = Socket()
    ws = xz.XiaozhiSocket(real, 's', pace=0.2)
    monkeypatch.setattr(xz, 'transcode', AsyncMock(return_value=[b'a', b'b', b'c']))
    await ws.send_json({'type': 'response', 'turn': 'q', 'text': 'speaking'})
    await ws.send_json({'type': 'audio', 'ref': 'q', 'mime': 'audio/mpeg', 'data': 'bXAz'})
    await until(lambda: b'a' in real.frames)
    await ws.abort()
    assert real.frames[-1]['state'] == 'stop'
    assert b'b' not in real.frames and ws.worker.cancelled()
    await ws.send_json({'type': 'audio', 'ref': 'q', 'mime': 'audio/mpeg', 'data': 'bXAz'})
    assert ws.waiting == {}
    # New speech still works after cancellation.
    await ws.send_json({'type': 'error', 'message': 'next'})
    await asyncio.wait_for(ws.worker, 1)
    assert real.frames[-2]['text'] == 'next'
    await ws.close()


@pytest.mark.asyncio
async def test_abort_only_stops_own_asking_turn(server, monkeypatch):
    adapter, client = server
    query = AsyncMock()
    monkeypatch.setattr(adapter, '_process_query', query)
    ws, _ = await connect(client)
    conn = adapter.registry.live('alice')[0]
    abort = AsyncMock()
    monkeypatch.setattr(conn.ws, 'abort', abort)
    other = Connection(Socket(), Identity('alice', 'other'))
    for turn in (None, Turn(other, 'q'), Turn(conn, 'unasked')):
        adapter._active_turn['alice'] = turn
        await ws.send_json({'type': 'abort'})
        await until(lambda: abort.await_count >= (1 if turn is None else 2 if turn.conn is other else 3))
        assert query.await_count == 0
    conn.asking['q'] = xz.time.monotonic()
    adapter._active_turn['alice'] = Turn(conn, 'q', started=True)
    await ws.send_json({'type': 'abort'})
    await until(lambda: query.await_count == 1)
    target, frame = query.call_args.args
    assert target is conn and frame['text'] == '/stop' and frame['id'] in conn.asking
    await ws.close()


@pytest.mark.asyncio
async def test_other_users_never_reach_device_and_release_requeues_unsent_pushes(server):
    adapter, client = server
    adapter.outbox.append('alice', 'queued reminder', 'cron', 'a')
    ws, _ = await connect(client)
    conn = adapter.registry.live('alice')[0]
    await until(lambda: 'a' in conn.unacked)
    await adapter._push('bob', 'private to bob', 'cron', 'b')
    assert set(conn.unacked) == {'a'}
    assert conn.ws.queue.empty() or conn.ws.queue.qsize() == 1
    assert [item[1] for item in adapter.outbox.drain('bob')] == ['private to bob']
    await ws.close()
    await until(lambda: not adapter.registry.live('alice'))
    await until(lambda: bool(adapter.outbox._items.get('alice')))
    assert not adapter.registry.by_user.get('alice')
    assert [item[1:] for item in adapter.outbox.drain('alice')] == [('queued reminder', 'cron', 'a')]


async def start_push(server, monkeypatch):
    adapter, client = server
    monkeypatch.setattr(adapter, '_speak', lambda *args: None)
    monkeypatch.setattr(xz, 'transcode', AsyncMock(return_value=[b'one', b'two']))
    ws, _ = await connect(client)
    conn = adapter.registry.live('alice')[0]
    conn.ws.pace = 0
    resume, send_bytes = asyncio.Event(), conn.ws.ws.send_bytes
    async def gated_send(packet):
        await send_bytes(packet)
        await resume.wait()
    monkeypatch.setattr(conn.ws.ws, 'send_bytes', gated_send)
    await adapter._push('alice', 'reminder', 'cron', 'p')
    await conn.send_json({'type': 'audio', 'ref': 'p', 'mime': 'audio/mpeg', 'data': 'bXAz'})
    assert (await ws.receive_json(timeout=1))['state'] == 'start'
    assert (await ws.receive_json(timeout=1))['state'] == 'sentence_start'
    assert (await ws.receive(timeout=1)).data == b'one'
    return ws, conn, resume


@pytest.mark.asyncio
async def test_mid_tts_disconnect_then_v0_held_disconnect_keeps_push(server, monkeypatch):
    adapter, client = server
    ws, _, _ = await start_push(server, monkeypatch)
    await ws.close()
    await until(lambda: not adapter.registry.by_user)
    monkeypatch.setattr(adapter, '_process_query', AsyncMock())
    legacy = await client.ws_connect('/voice?token=tok-alice-1')
    assert (await legacy.receive_json(timeout=1))['type'] == 'welcome'
    await legacy.send_json({'type': 'query', 'id': 'q', 'text': 'question in flight'})
    await until(lambda: bool(adapter.registry.live('alice')[0].held))
    await legacy.close()
    await until(lambda: not adapter.registry.by_user)
    assert [item[3] for item in adapter.outbox._items['alice']] == ['p']
    assert json.loads(adapter.outbox.path.read_text())['alice'][0][3] == 'p'
    assert 'p' not in adapter._delivered.get('alice', ())


@pytest.mark.asyncio
async def test_completed_tts_push_marks_delivered_and_removes_persisted_outbox(server, monkeypatch):
    adapter, _ = server
    ws, conn, resume = await start_push(server, monkeypatch)
    assert json.loads(adapter.outbox.path.read_text())['alice'][0][3] == 'p'
    assert 'p' not in adapter._delivered.get('alice', ())
    resume.set()
    assert (await ws.receive(timeout=1)).data == b'two'
    assert (await ws.receive_json(timeout=1))['state'] == 'stop'
    await asyncio.wait_for(conn.ws.worker, 1)
    assert 'p' in adapter._delivered['alice'] and not conn.unacked
    assert json.loads(adapter.outbox.path.read_text()) == {}
    await ws.close()
    await until(lambda: not adapter.registry.by_user)
    assert not adapter.outbox._items.get('alice')


@pytest.mark.asyncio
async def test_aborted_push_requeued_on_close(server, monkeypatch):
    adapter, _ = server
    ws, conn, _ = await start_push(server, monkeypatch)
    await ws.send_json({'type': 'abort'})
    assert (await ws.receive_json(timeout=1))['state'] == 'stop'
    assert conn.ws.worker.cancelled()
    await ws.close()
    await until(lambda: not adapter.registry.by_user)
    assert [item[3] for item in adapter.outbox._items['alice']] == ['p']
    assert json.loads(adapter.outbox.path.read_text())['alice'][0][3] == 'p'
    assert 'p' not in adapter._delivered.get('alice', ())


@pytest.mark.asyncio
async def test_large_transcode_demux_runs_off_event_loop(monkeypatch):
    output = xz.mux_opus([b'x' * 1000] * 40)
    child = AsyncMock()
    child.returncode = 0
    child.communicate.return_value = (output, b'')
    monkeypatch.setattr(xz.asyncio, 'create_subprocess_exec', AsyncMock(return_value=child))
    original, threads = xz.demux_opus, []
    def capture(data):
        threads.append(threading.get_ident())
        return original(data)
    monkeypatch.setattr(xz, 'demux_opus', capture)
    assert await xz.transcode(b'mp3') == [b'x' * 1000] * 40
    assert threads and all(thread != threading.get_ident() for thread in threads)


@pytest.mark.asyncio
async def test_simulator_round_trip_against_fake_adapter(server, monkeypatch, tmp_path):
    adapter, client = server
    spec = importlib.util.spec_from_file_location('xiaozhi_sim', Path(__file__).resolve().parents[1] / 'sim/xiaozhi_sim.py')
    sim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sim)
    captured = []
    async def fake_query(conn, frame):
        captured.append(demux_opus(base64.b64decode(frame['audio']['data'])))
        await conn.send_json({'type': 'transcript', 'turn': frame['id'], 'text': 'hello'})
        await conn.send_json({'type': 'response', 'turn': frame['id'], 'text': 'reply'})
        await conn.send_json({'type': 'audio', 'ref': frame['id'], 'mime': 'audio/mpeg', 'data': 'bXAz'})
    monkeypatch.setattr(adapter, '_process_query', fake_query)
    monkeypatch.setattr(sim, 'wav_packets', AsyncMock(return_value=[SPEECH, QUIET]))
    monkeypatch.setattr(xz, 'transcode', AsyncMock(return_value=[b'reply-opus']))
    out, frames = tmp_path / 'reply.ogg', []
    ota = str(client.make_url('/xiaozhi/ota/tok-alice-1'))
    assert await sim.run(ota=ota, out=out, timeout=2, emit=lambda s: frames.append(json.loads(s))) == 0
    assert captured == [[SPEECH, QUIET]] and demux_opus(out.read_bytes()) == [b'reply-opus']
    assert [f['type'] for f in frames] == ['hello', 'stt', 'tts', 'tts', 'tts']
    assert frames[-2]['text'] == 'reply'


@pytest.mark.asyncio
async def test_simulator_waits_past_a_queued_reminder_for_its_own_answer(server, monkeypatch, tmp_path):
    """#45: the reminder played on connect ends with its own `tts stop`; the run must still send its query."""
    adapter, client = server
    spec = importlib.util.spec_from_file_location('xiaozhi_reminder_sim', Path(__file__).resolve().parents[1] / 'sim/xiaozhi_sim.py')
    sim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sim)
    adapter.outbox.append('alice', 'reminder', 'cron', 'p')
    monkeypatch.setattr(xz, 'AUDIO_TIMEOUT', 0.01)
    captured = []
    async def fake_query(conn, frame):
        captured.append(frame['id'])
        await conn.send_json({'type': 'transcript', 'turn': frame['id'], 'text': 'hello'})
        await conn.send_json({'type': 'response', 'turn': frame['id'], 'text': 'reply'})
    monkeypatch.setattr(adapter, '_process_query', fake_query)
    monkeypatch.setattr(sim, 'wav_packets', AsyncMock(return_value=[SPEECH, QUIET]))
    frames = []
    assert await sim.run(ota=str(client.make_url('/xiaozhi/ota/tok-alice-1')), out=tmp_path / 'reply.ogg',
                         timeout=3, emit=lambda s: frames.append(json.loads(s))) == 0
    texts = [f.get('text') for f in frames if f.get('state') == 'sentence_start']
    assert captured and texts == ['reminder', 'reply']
    assert frames[-1] == {**frames[-1], 'type': 'tts', 'state': 'stop'}


@pytest.mark.asyncio
async def test_simulator_ends_on_a_spoken_error_without_an_stt_frame(server, monkeypatch, tmp_path):
    """Failed transcription: the adapter speaks an error and sends no `stt`; the run still ends on its `tts stop`."""
    adapter, client = server
    spec = importlib.util.spec_from_file_location('xiaozhi_error_sim', Path(__file__).resolve().parents[1] / 'sim/xiaozhi_sim.py')
    sim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sim)
    monkeypatch.setattr(xz, 'AUDIO_TIMEOUT', 0.01)
    async def fake_query(conn, frame):
        await conn.send_json({'type': 'error', 'turn': frame['id'], 'message': "I couldn't hear that."})
    monkeypatch.setattr(adapter, '_process_query', fake_query)
    monkeypatch.setattr(sim, 'wav_packets', AsyncMock(return_value=[SPEECH, QUIET]))
    frames = []
    started = asyncio.get_running_loop().time()
    assert await sim.run(ota=str(client.make_url('/xiaozhi/ota/tok-alice-1')), out=tmp_path / 'reply.ogg',
                         timeout=3, emit=lambda s: frames.append(json.loads(s))) == 0
    assert asyncio.get_running_loop().time() - started < 2
    assert "couldn't hear" in ' '.join(str(f.get('text')) for f in frames)


@pytest.mark.asyncio
async def test_simulator_text_only_receives_reminder_without_upload(server, monkeypatch, tmp_path):
    adapter, client = server
    spec = importlib.util.spec_from_file_location('xiaozhi_text_sim', Path(__file__).resolve().parents[1] / 'sim/xiaozhi_sim.py')
    sim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sim)
    adapter.outbox.append('alice', 'reminder', 'cron', 'p')
    monkeypatch.setattr(xz, 'AUDIO_TIMEOUT', 0.01)
    wav = AsyncMock(side_effect=AssertionError('must not encode'))
    query = AsyncMock()
    monkeypatch.setattr(sim, 'wav_packets', wav)
    monkeypatch.setattr(adapter, '_process_query', query)
    frames = []
    out = tmp_path / 'not-written.ogg'
    assert await sim.run(ota=str(client.make_url('/xiaozhi/ota/tok-alice-1')), text_only=True,
                         out=out, timeout=2, emit=frames.append) == 0
    assert not out.exists() and not wav.called and not query.called
    assert json.loads(frames[-2])['text'] == 'reminder'


def test_cli_xiaozhi_ota_url_no_websocket_or_qr(monkeypatch, capsys):
    entries = []
    monkeypatch.setattr(cli, '_entries', lambda: entries)
    monkeypatch.setattr(cli, '_save', lambda e: entries.extend(e))
    def setting(key, default=''):
        return ','.join(f'{u}:{d}={t}' for u, d, t in entries) if key == 'devices' else default
    monkeypatch.setattr(cli, '_setting', setting)
    monkeypatch.setattr(cli, '_qr', lambda url: pytest.fail('xiaozhi must not print QR'))
    parser = argparse.ArgumentParser()
    cli.setup(parser)
    assert cli.main(parser.parse_args(['pair', 'alice', 'box', '--xiaozhi', '--host', 'host.example', '--port', '9000'])) == 0
    output = capsys.readouterr().out
    assert 'http://host.example:9000/xiaozhi/ota/' + entries[0][2] in output
    assert 'OTA address' in output and 'Wi-Fi setup' in output
    assert 'ws://' not in output and 'wss://' not in output and 'scan' not in output


@pytest.mark.asyncio
async def test_real_ffmpeg_mpeg_conversion_and_simulator_wav():
    import shutil
    import subprocess
    if not shutil.which('ffmpeg'):
        pytest.skip('ffmpeg not installed')
    spec = importlib.util.spec_from_file_location('xiaozhi_codec_sim', Path(__file__).resolve().parents[1] / 'sim/xiaozhi_sim.py')
    sim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sim)
    uploaded = await sim.wav_packets(sim.DEFAULT_WAV)
    assert len(uploaded) > 20
    mp3 = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(sim.DEFAULT_WAV), '-f', 'mp3', 'pipe:1'],
                         capture_output=True, check=True).stdout
    reply = await xz.transcode(mp3)
    assert len(reply) > 20
    # Opus TOC: code 3 packs three 20 ms frames, hence a 60 ms packet.
    assert all(packet[0] & 3 == 3 and packet[1] & 0x3f == 3 for packet in reply)


@pytest.mark.asyncio
async def test_conversion_cancel_kills_and_reaps_child(monkeypatch):
    entered = asyncio.Event()
    class Process:
        returncode = None
        killed = False
        reaped = False
        async def communicate(self, data):
            entered.set()
            await asyncio.Event().wait()
        def kill(self):
            self.killed = True
        async def wait(self):
            self.reaped = True
            self.returncode = -9
    child = Process()
    monkeypatch.setattr(xz.asyncio, 'create_subprocess_exec', AsyncMock(return_value=child))
    task = asyncio.create_task(xz.transcode(b'mp3'))
    await entered.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert child.killed and child.reaped


@pytest.mark.asyncio
async def test_interleaved_push_does_not_repeat_thinking_for_same_turn():
    real = Socket()
    ws = xz.XiaozhiSocket(real, 's', audio_timeout=0.01)
    await ws.send_json({'type': 'status', 'state': 'thinking'})
    await ws.send_json({'type': 'push', 'id': 'p', 'text': 'a reminder'})
    await ws.send_json({'type': 'status', 'state': 'working'})
    assert len([f for f in real.frames if f['type'] == 'llm']) == 1
    await ws.close()


@pytest.mark.parametrize('frame_ms,size,speech', [(60, 31, True), (60, 30, False), (20, 13, True), (20, 40, True),
                                                  (20, 12, False)])
def test_speech_detection_follows_the_frame_duration(frame_ms, size, speech):
    from glasses_plugin.xiaozhi import Recording
    recording = Recording({'sample_rate': 16000, 'channels': 1, 'frame_duration': frame_ms})
    recording.add(b'x' * size)
    assert recording.speech is speech


def test_long_frames_of_silence_end_an_auto_recording():
    from glasses_plugin.xiaozhi import Recording
    recording = Recording({'sample_rate': 16000, 'channels': 1, 'frame_duration': 120}, mode='auto')
    recording.add(b'x' * 200)  # speech
    assert any(recording.add(b'x' * 14) for _ in range(7))  # 120 ms silence packets: 840 ms of quiet ends it


# Packet sizes measured from ffmpeg/libopus 16 kb/s VBR: (frame ms, median speech packet, digital-silence packet).
@pytest.mark.parametrize('frame_ms,speech,silence', [(20, 40, 8), (40, 80, 15), (60, 119, 21), (80, 147, 29),
                                                     (100, 178, 37), (120, 225, 41)])
def test_vbr_silence_ends_an_auto_recording_after_800_ms(frame_ms, speech, silence):
    from glasses_plugin.xiaozhi import Recording
    recording = Recording({'sample_rate': 16000, 'channels': 1, 'frame_duration': frame_ms}, mode='auto')
    assert not any(recording.add(b'x' * speech) for _ in range(20))
    ends = [recording.add(b'x' * silence) for _ in range(-(-800 // frame_ms))]
    assert ends[-1] and not any(ends[:-1])  # exactly when 800 ms of quiet has accumulated
    assert recording.speech
