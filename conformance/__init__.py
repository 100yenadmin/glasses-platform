"""Protocol-v1 interoperability tools; only aiohttp and the standard library."""
import asyncio
import base64
import io
import json
import wave
from uuid import uuid4
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import aiohttp
from aiohttp import web


def token_url(url, token):
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != 'token']
    return urlunsplit(parts._replace(query=urlencode(query + [('token', token)])))


class Report:
    def __init__(self):
        self.checks = []

    def add(self, name, ok, detail=''):
        self.checks.append({'check': name, 'status': 'PASS' if ok else 'FAIL', 'detail': detail})

    def skip(self, name, detail):
        self.checks.append({'check': name, 'status': 'SKIP', 'detail': detail})

    @property
    def ok(self):
        return all(item['status'] != 'FAIL' for item in self.checks)

    def as_dict(self):
        return {'ok': self.ok, 'checks': self.checks}

    def print(self, json_output=False):
        if json_output:
            print(json.dumps(self.as_dict()), flush=True)
        else:
            for item in self.checks:
                print(f"{item['status']} {item['check']}" + (f": {item['detail']}" if item['detail'] else ''), flush=True)


async def receive(ws, kind, timeout=10):
    async with asyncio.timeout(timeout):
        while True:
            msg = await ws.receive()
            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                raise ConnectionError('Socket closed before ' + kind)
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                frame = json.loads(msg.data)
            except ValueError:
                continue
            if isinstance(frame, dict) and frame.get('type') == kind:
                return frame


async def check_server(url, token, http=None, full=False, timeout=10):
    """Return every completed check, including a causal failure on transport errors.

    Exactly-once is observed through a subsequent ping/pong barrier, not forever.
    Full mode schedules a real reminder on the target agent.
    """
    report = Report()
    planned = ['welcome protocol_max >= 1', 'hello_ack protocol == 1',
               'hello_ack identity and version', 'hello_ack server.features list',
               'malformed and unknown frames keep socket open; ping/pong',
               'query yields exactly one matching response', 'new_session yields session_reset']
    if full:
        planned.append('reminder push within 150 seconds')
    stage = 'bad token closes 4401'
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        try:
            bad = token_url(url, 'invalid-' + uuid4().hex)
            async with session.ws_connect(bad) as ws:
                async with asyncio.timeout(timeout):
                    while not ws.closed:
                        await ws.receive()
                report.add(stage, ws.close_code == 4401)
        except (aiohttp.ClientError, TimeoutError, ConnectionError):
            report.add(stage, False, 'Did not receive WebSocket close 4401.')
        stage = 'connect'
        try:
            async with session.ws_connect(token_url(url, token)) as ws:
                stage = 'welcome protocol_max >= 1'
                welcome = await receive(ws, 'welcome', timeout)
                maximum = welcome.get('protocol_max')
                report.add(stage, type(maximum) is int and maximum >= 1)
                await ws.send_json({'type': 'hello', 'protocol': 1,
                                    'device': {'name': 'conformance', 'capabilities': {}}})
                stage = 'hello_ack protocol == 1'
                hello = await receive(ws, 'hello_ack', timeout)
                report.add(stage, type(hello.get('protocol')) is int and hello['protocol'] == 1)
                server = hello.get('server', {})
                report.add('hello_ack identity and version',
                           all(isinstance(hello.get(k), str) and hello[k] for k in ('user', 'device'))
                           and isinstance(server, dict) and isinstance(server.get('version'), str)
                           and bool(server.get('version')))
                report.add('hello_ack server.features list',
                           isinstance(server, dict) and isinstance(server.get('features'), list),
                           '' if isinstance(server, dict) and isinstance(server.get('features'), list)
                           else 'hello_ack requires server.features list.')
                stage = 'malformed and unknown frames keep socket open; ping/pong'
                for raw in ('not json', '[]', '{"type":[]}', '{"type":"future","extra":true}'):
                    await ws.send_str(raw)
                await ws.send_json({'type': 'ping'})
                await receive(ws, 'pong', timeout)
                report.add(stage, True)
                stage = 'query yields exactly one matching response'
                turn = 'check-' + uuid4().hex
                await ws.send_json({'type': 'query', 'id': turn, 'text': 'Say hello.', 'speak': False})
                reply = await receive(ws, 'response', timeout)
                matches = (reply.get('turn') == turn and isinstance(reply.get('text'), str)
                           and reply.get('final') is True)
                await ws.send_json({'type': 'ping'})
                async with asyncio.timeout(timeout):
                    while True:
                        frame = await ws.receive_json()
                        if frame.get('type') == 'response' and frame.get('turn') == turn:
                            matches = False
                        if frame.get('type') == 'pong':
                            break
                report.add(stage, matches)
                stage = 'new_session yields session_reset'
                await ws.send_json({'type': 'new_session'})
                await receive(ws, 'session_reset', timeout)
                report.add(stage, True)
                if full:
                    stage = 'reminder push within 150 seconds'
                    await ws.send_json({'type': 'query', 'id': 'reminder-' + uuid4().hex,
                                        'text': 'Remind me in one minute to stretch.'})
                    async with asyncio.timeout(150):
                        while True:
                            push = await receive(ws, 'push', 150)
                            if push.get('origin') == 'cron':
                                break
                    report.add(stage, isinstance(push.get('id'), str) and isinstance(push.get('text'), str))
        except (aiohttp.ClientError, TimeoutError, ConnectionError, ValueError, TypeError):
            report.add(stage, False, 'Expected frame missing, malformed, or connection failed.')
        completed = {item['check'] for item in report.checks}
        for name in planned:
            if name not in completed:
                report.skip(name, 'Not reached after ' + stage + ' failed.')
        if http:
            base = http.rstrip('/')
            headers = {'Authorization': 'Bearer ' + token}
            for name, method, path, options, predicate in (
                ('HTTP requires token', 'POST', '/turn', {'json': {'text': 'hello'}}, lambda r, b: r.status == 401),
                ('HTTP malformed body is 400', 'POST', '/turn', {'headers': headers, 'data': '{'}, lambda r, b: r.status == 400),
                ('HTTP pending empty-list shape', 'GET', '/pending?wait=0', {'headers': headers},
                 lambda r, b: r.status == 200 and isinstance(b, dict) and b.get('items') == []),
            ):
                try:
                    async with session.request(method, base + path, timeout=aiohttp.ClientTimeout(total=timeout), **options) as response:
                        try:
                            body = await response.json()
                        except (ValueError, aiohttp.ContentTypeError):
                            body = None
                        report.add(name, predicate(response, body))
                except (aiohttp.ClientError, TimeoutError):
                    report.add(name, False, 'HTTP request failed.')
    return report


def wav_data():
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b'\0\0' * 80)
    return base64.b64encode(output.getvalue()).decode()


def fake_app(token, *, on_report=None, scenario_timeout=20):
    """Scripted client acceptance peer. Reports only checks, never received credentials/data."""
    app = web.Application()
    reports = app['reports'] = []

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        auth = request.headers.get('Authorization', '')
        supplied = auth[7:] if auth.lower().startswith('bearer ') else request.query.get('token')
        if supplied != token:
            await ws.close(code=4401, message=b'unauthorized')
            return ws
        report = Report()
        try:
            await ws.send_json({'type': 'welcome', 'protocol_max': 1})
            hello = await receive(ws, 'hello', 2)
            device = hello.get('device', {})
            caps = device.get('capabilities', {})
            valid = hello.get('protocol') == 1 and isinstance(device.get('name'), str) and isinstance(caps, dict)
            report.add('hello', valid)
            if not valid:
                raise ValueError('Invalid hello')
            await ws.send_json({'type': 'hello_ack', 'protocol': 1, 'user': 'fixture',
                                'device': device['name'], 'server': {'version': 'fixture',
                                'features': ['audio_in', 'audio_out', 'approval_buttons', 'ack', 'display', 'haptic']}})
            expected = {}
            await ws.send_json({'type': 'push', 'id': 'push-1', 'origin': 'message', 'text': 'Hello device.'})
            if caps.get('audio_out'):
                await ws.send_json({'type': 'audio', 'ref': 'push-1', 'mime': 'audio/wav', 'data': wav_data()})
            if caps.get('ack'):
                expected['ack'] = 'push-1'
            if caps.get('camera'):
                expected['photo'] = 'photo-1'
                await ws.send_json({'type': 'capture_photo', 'request': 'photo-1', 'reason': 'tool'})
            if caps.get('buttons'):
                expected['approval_response'] = 'approval-1'
                await ws.send_json({'type': 'approval_request', 'id': 'approval-1', 'text': 'Allow fixture?',
                                    'command': 'fixture', 'description': 'No operation is performed.', 'choices': ['once', 'deny']})
            if caps.get('display'):
                await ws.send_json({'type': 'display', 'text': 'Fixture display'})
            if caps.get('haptics'):
                await ws.send_json({'type': 'haptic', 'pattern': 'tap'})
            await ws.send_json({'type': 'future', 'ignored': True})
            queried = False
            async with asyncio.timeout(scenario_timeout):
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    frame = json.loads(msg.data)
                    kind = frame.get('type')
                    key = 'photo' if kind == 'photo_error' else kind
                    if key in expected:
                        identifier = frame.get('request') if key == 'photo' else frame.get('id')
                        ok = identifier == expected[key]
                        if kind == 'photo':
                            try:
                                raw = base64.b64decode(frame.get('data', ''), validate=True)
                                ok = ok and raw.startswith(b'\xff\xd8') and raw.endswith(b'\xff\xd9')
                            except (ValueError, TypeError):
                                ok = False
                        if key == 'approval_response':
                            ok = ok and frame.get('choice') in ('once', 'deny')
                            await ws.send_json({'type': 'approval_result', 'id': identifier, 'ok': ok,
                                                'choice': frame.get('choice'), 'message': 'Fixture answer recorded.'})
                            await ws.send_json({'type': 'approval_closed', 'id': identifier})
                        report.add(key, ok)
                        del expected[key]
                    elif kind == 'query':
                        queried = True
                        turn = frame.get('id')
                        report.add('query', isinstance(turn, str) and bool(turn))
                        if frame.get('audio'):
                            await ws.send_json({'type': 'transcript', 'turn': turn, 'text': 'Fixture transcript'})
                        await ws.send_json({'type': 'status', 'state': 'thinking'})
                        await ws.send_json({'type': 'response_delta', 'turn': turn, 'text': 'Fixture'})
                        await ws.send_json({'type': 'response', 'turn': turn, 'text': 'Fixture reply', 'final': True, 'tts': False})
                        if caps.get('audio_out') and frame.get('speak') is not False:
                            await ws.send_json({'type': 'audio', 'ref': turn, 'mime': 'audio/wav', 'data': wav_data()})
                    elif kind == 'ping':
                        await ws.send_json({'type': 'pong'})
                    elif kind == 'new_session':
                        await ws.send_json({'type': 'session_reset'})
                    if queried and not expected:
                        break
            for key in expected:
                report.add(key, False, 'Missing client answer.')
            if not queried:
                report.add('query', False, 'No query received.')
        except (TimeoutError, ConnectionError, ValueError, TypeError, AttributeError):
            report.add('scenario', False, 'Client did not complete the scripted exchange.')
        finally:
            reports.append(report)
            if on_report:
                on_report(report)
            await ws.close()
        return ws

    app.router.add_get('/voice', handler)
    return app
