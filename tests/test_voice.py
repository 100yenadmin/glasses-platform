"""Voice runs through fake Hermes providers; no provider or live gateway is used."""
import asyncio
import base64
import io
import json
import sys
import types
import wave
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from glasses_plugin import audio, protocol
from glasses_plugin.devices import Connection, Identity


class Socket:
    closed = False

    def __init__(self):
        self.frames = []

    async def send_json(self, frame):
        self.frames.append(frame)


async def device(adapter, name='ray', user='alice', version=1, **capabilities):
    conn = Connection(Socket(), Identity(user, name), protocol=version, capabilities=capabilities, flushed=True)
    await adapter.registry.add(conn)
    adapter._loop = asyncio.get_running_loop()
    return conn


async def drained(adapter):
    while adapter._background_tasks:
        await asyncio.gather(*list(adapter._background_tasks))


@pytest.fixture
def speech(monkeypatch, tmp_path):
    stt = types.ModuleType('tools.transcription_tools')
    stt.transcribe_audio = Mock(return_value={'success': True, 'transcript': 'hello there'})
    tts = types.ModuleType('tools.tts_tool')
    path = tmp_path / 'speech.mp3'
    path.write_bytes(b'mp3 fixture')
    tts.text_to_speech_tool = Mock(return_value=json.dumps({'success': True, 'file_path': str(path)}))
    monkeypatch.setitem(sys.modules, stt.__name__, stt)
    monkeypatch.setitem(sys.modules, tts.__name__, tts)
    cache = AsyncMock(return_value=str(tmp_path / 'input.wav'))
    monkeypatch.setattr('gateway.platforms.base.cache_audio_from_bytes_async', cache)
    return stt.transcribe_audio, tts.text_to_speech_tool, cache


def recording(mime='audio/wav', data=b'fake recording', **extra):
    return {'mime': mime, 'data': base64.b64encode(data).decode(), **extra}


@pytest.mark.asyncio
async def test_audio_is_transcribed_before_normal_text_handling(make_adapter, speech):
    adapter = make_adapter()
    conn = await device(adapter)
    adapter.handle_message = AsyncMock()
    await adapter._process_query(conn, {'id': 'q1', 'audio': recording()})
    assert conn.ws.frames == [protocol.transcript('q1', 'hello there')]
    event = adapter.handle_message.call_args.args[0]
    assert event.text == 'hello there' and 'audio' not in event.raw_message
    assert speech[2].call_args.args == (b'fake recording', '.wav')
    speech[0].assert_called_once_with(speech[2].return_value)


def test_pcm_is_little_endian_mono_wav():
    data, ext = audio.decode(recording('audio/pcm', b'\x01\x00\xff\x7f'), 100)
    with wave.open(io.BytesIO(data), 'rb') as handle:
        assert (handle.getnchannels(), handle.getsampwidth(), handle.getframerate()) == (1, 2, 16000)
        assert handle.readframes(2) == b'\x01\x00\xff\x7f'
    assert ext == '.wav'


@pytest.mark.parametrize('mime,ext', list(audio.MIME_EXT.items()))
def test_input_mime_extensions(mime, ext):
    assert audio.decode(recording(mime), 100)[1] == ext


@pytest.mark.parametrize('rate', [8000, 48000])
def test_pcm_rate_boundaries(rate):
    data, _ = audio.decode(recording('audio/pcm', b'\0\0', rate=rate), 100)
    with wave.open(io.BytesIO(data), 'rb') as handle:
        assert handle.getframerate() == rate


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['stt', 'empty', 'exception', 'mime', 'size', 'base64', 'rate', 'odd_pcm'])
async def test_bad_audio_answers_with_error(make_adapter, speech, failure):
    adapter = make_adapter()
    conn = await device(adapter)
    conn.asking['q1'] = 1
    adapter.handle_message = AsyncMock()
    value = recording()
    if failure == 'stt':
        speech[0].return_value = {'success': False, 'error': 'fake'}
    elif failure == 'empty':
        speech[0].return_value = {'success': True, 'transcript': '  '}
    elif failure == 'exception':
        speech[0].side_effect = RuntimeError('fake')
    elif failure == 'mime':
        value['mime'] = 'text/plain'
    elif failure == 'size':
        adapter.broker.max_bytes = 2
    elif failure == 'base64':
        value['data'] = '!!'
    elif failure == 'rate':
        value = recording('audio/pcm', b'\0\0', rate=7999)
    else:
        value = recording('audio/pcm', b'\0')
    await adapter._process_query(conn, {'id': 'q1', 'audio': value})
    assert conn.ws.frames == [protocol.error("I couldn't hear that.")]
    assert not conn.asking
    adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_v0_ignores_audio(make_adapter, speech):
    adapter = make_adapter()
    conn = await device(adapter, version=0)
    adapter.handle_message = AsyncMock()
    await adapter._process_query(conn, {'id': 'q', 'text': 'typed words', 'audio': recording()})
    assert adapter.handle_message.call_args.args[0].text == 'typed words'
    speech[0].assert_not_called()
    assert not conn.ws.frames


@pytest.mark.asyncio
async def test_response_then_audio_uses_delivered_text(make_adapter, speech):
    adapter = make_adapter()
    conn = await device(adapter, audio_out=['audio/mpeg'])
    await adapter._deliver_reply('alice', conn, 'q1', 'The reply.\nA media note.')
    await drained(adapter)
    assert [f['type'] for f in conn.ws.frames] == ['response', 'audio']
    assert conn.ws.frames[1] == protocol.audio('q1', base64.b64encode(b'mp3 fixture').decode(), 'audio/mpeg')
    speech[1].assert_called_once_with(conn.ws.frames[0]['text'])


@pytest.mark.asyncio
async def test_speak_false_is_per_query(make_adapter, speech):
    adapter = make_adapter()
    conn = await device(adapter, audio_out=['audio/mpeg'])
    adapter.handle_message = AsyncMock()
    for id, speak in [('silent', False), ('spoken', True)]:
        await adapter._process_query(conn, {'id': id, 'text': 'hi', 'speak': speak})
        await adapter._deliver_reply('alice', conn, id, id)
    await drained(adapter)
    assert [f['ref'] for f in conn.ws.frames if f['type'] == 'audio'] == ['spoken']


@pytest.mark.asyncio
@pytest.mark.parametrize('delayed', [False, True])
async def test_silent_reply_stays_silent_if_asker_disconnects(make_adapter, speech, delayed):
    adapter = make_adapter()
    conn = await device(adapter, audio_out=['audio/mpeg'])
    adapter.handle_message = AsyncMock()
    await adapter._process_query(conn, {'id': 'q1', 'text': 'hello', 'speak': False})
    adapter._release(conn)
    other = None if delayed else await device(adapter, 'phone', audio_out=['audio/mpeg'])
    await adapter._deliver_reply('alice', conn, 'q1', 'The reply')
    if delayed:
        other = await device(adapter, 'phone', audio_out=['audio/mpeg'])
        other.flushed = False
        await adapter._flush_outbox(other)
    await drained(adapter)
    assert [f['type'] for f in other.ws.frames] == ['push']
    speech[1].assert_not_called()


@pytest.mark.asyncio
async def test_push_synthesised_once_for_two_devices_and_capability_gate(make_adapter, speech):
    adapter = make_adapter()
    a = await device(adapter, audio_out=['audio/mpeg'])
    b = await device(adapter, 'phone', audio_out=['audio/mpeg'])
    silent = await device(adapter, 'silent')
    legacy = await device(adapter, 'legacy', version=0, audio_out=['audio/mpeg'])
    await adapter._push('alice', 'A reminder.', 'cron', 'p1')
    await drained(adapter)
    for conn in [a, b]:
        assert [f['type'] for f in conn.ws.frames] == ['push', 'audio']
        assert conn.ws.frames[-1]['ref'] == 'p1'
    assert [f['type'] for f in silent.ws.frames] == ['push']
    assert [f['type'] for f in legacy.ws.frames] == ['response']
    speech[1].assert_called_once_with('A reminder.')


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['outbox', 'pending', 'held'])
async def test_speech_for_delayed_pushes(make_adapter, speech, path):
    adapter = make_adapter()
    conn = await device(adapter, audio_out=['audio/mpeg'])
    if path == 'outbox':
        adapter.outbox.append('alice', 'Delayed', 'message', 'p1')
        conn.flushed = False
        await adapter._flush_outbox(conn)
    elif path == 'pending':
        conn.flushed = False
        await adapter._push('alice', 'Delayed', 'message', 'p1')
        await adapter._flush_outbox(conn)
    else:
        conn.held = [(1e12, 'Delayed', 'message', 'p1')]
        await adapter._release_held(conn)
    await drained(adapter)
    assert [f['type'] for f in conn.ws.frames] == ['push', 'audio']
    assert conn.ws.frames[-1]['ref'] == 'p1'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['error', 'empty', 'raise', 'missing_file'])
async def test_tts_failure_keeps_text_only(make_adapter, speech, failure):
    adapter = make_adapter()
    conn = await device(adapter, audio_out=['audio/mpeg'])
    if failure == 'raise':
        speech[1].side_effect = RuntimeError('fake')
    else:
        speech[1].return_value = json.dumps({'error': 'fake'} if failure == 'error' else
                                            {'file_path': '/does-not-exist'} if failure == 'missing_file' else {})
    await adapter._push('alice', 'Still delivered', 'message', 'p1')
    await drained(adapter)
    assert [f['type'] for f in conn.ws.frames] == ['push']


@pytest.mark.asyncio
async def test_synthesis_starts_after_user_lock_released(make_adapter, speech):
    adapter = make_adapter()
    conn = await device(adapter, audio_out=['audio/mpeg'])
    observed = []
    original = speech[1].return_value
    def synth(text):
        observed.append(adapter._lock('alice').locked())
        return original
    speech[1].side_effect = synth
    async with adapter._lock('alice'):
        adapter._speak(conn, 'hello', 'q')
        await asyncio.sleep(0)
        speech[1].assert_not_called()
    await drained(adapter)
    assert observed == [False]


@pytest.mark.asyncio
async def test_mime_negotiation_and_transcoding_use_exec_not_shell(monkeypatch):
    monkeypatch.setattr(audio.shutil, 'which', lambda _: '/fake/ffmpeg')
    assert audio.output_mime({'audio_out': ['unsupported', 'audio/ogg', 'audio/mpeg']}) == 'audio/ogg'
    proc = types.SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b'encoded', b'')))
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(audio.asyncio, 'create_subprocess_exec', spawn)
    for mime, container in [('audio/ogg', 'ogg'), ('audio/wav', 'wav'), ('audio/mpeg', 'mp3')]:
        assert await audio.encode_files(['one.mp3', 'two.mp3'], mime) == b'encoded'
        args = spawn.call_args.args
        assert args[0] == '/fake/ffmpeg' and args.count('-i') == 2
        assert '[0:a][1:a]concat=n=2:v=0:a=1[out]' in args
        assert args[-3:] == ('-f', container, 'pipe:1')
    monkeypatch.setattr(audio.shutil, 'which', lambda _: None)
    assert audio.output_mime({'audio_out': ['audio/ogg', 'audio/wav', 'audio/mpeg']}) == 'audio/mpeg'
    assert audio.output_mime({'audio_out': ['audio/ogg']}) is None
    assert audio.output_mime({'audio_out': 'audio/mpeg'}) is None


@pytest.mark.asyncio
async def test_multiple_native_chunks_without_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(audio.shutil, 'which', lambda _: None)
    paths = [tmp_path / 'one.mp3', tmp_path / 'two.mp3']
    for path, data in zip(paths, [b'one', b'two']):
        path.write_bytes(data)
    assert await audio.encode_files(paths, 'audio/mpeg') == b'onetwo'


def test_hello_ack_advertises_real_version_and_features():
    manifest = Path(protocol.__file__).with_name('plugin.yaml').read_text()
    frame = protocol.hello_ack('alice', 'ray')
    assert f'version: "{frame["server"]["version"]}"' in manifest
    assert frame['server']['features'] == ['audio_in', 'audio_out', 'approval_buttons', 'ack', 'display', 'haptic']
