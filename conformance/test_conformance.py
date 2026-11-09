import asyncio
import importlib
from unittest.mock import AsyncMock
import sys

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

# tests/conftest.py deliberately removes the repo root from sys.path.
kit = importlib.import_module('glasses_plugin.conformance')


@pytest.mark.asyncio
async def test_quick_against_existing_adapter(server):
    adapter, client = server
    from gateway.platforms.event import ProcessingOutcome

    async def reply(event):
        await adapter.on_processing_start(event)
        await adapter.send(event.source.chat_id, 'Fixture reply', metadata={'notify': True})
        await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    adapter.handle_message = AsyncMock(side_effect=reply)
    report = await kit.check_server(str(client.make_url('/voice')).replace('http:', 'ws:'), 'tok-alice-1')
    failures = [item for item in report.checks if item['status'] == 'FAIL']
    assert report.ok, report.as_dict()


@pytest.mark.asyncio
async def test_broken_server_has_clear_failure():
    async def broken(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.close(code=1000)
        return ws

    app = web.Application()
    app.router.add_get('/voice', broken)
    async with TestServer(app) as server:
        report = await kit.check_server(str(server.make_url('/voice')).replace('http:', 'ws:'), 'fixture', timeout=0.3)
    assert not report.ok
    assert any(item['check'] == 'bad token closes 4401' and item['status'] == 'FAIL' for item in report.checks)
    assert any(item['check'].startswith('welcome') and item['status'] == 'FAIL' for item in report.checks)
    assert any(item['check'] == 'new_session yields session_reset' and item['status'] == 'SKIP'
               for item in report.checks)


@pytest.mark.asyncio
async def test_complete_checker_including_http_and_duplicate_detection():
    duplicate = False

    async def socket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if request.query.get('token') != 'fixture':
            await ws.close(code=4401)
            return ws
        await ws.send_json({'type': 'welcome', 'protocol_max': 1})
        async for msg in ws:
            try:
                frame = msg.json()
                kind = frame.get('type')
            except (ValueError, AttributeError):
                continue
            if kind == 'hello':
                await ws.send_json({'type': 'hello_ack', 'protocol': 1, 'user': 'fixture', 'device': 'fixture',
                                    'server': {'version': 'fixture', 'features': []}})
            elif kind == 'ping':
                await ws.send_json({'type': 'pong'})
            elif kind == 'query':
                response = {'type': 'response', 'turn': frame['id'], 'text': 'hello', 'final': True}
                await ws.send_json(response)
                if duplicate:
                    await ws.send_json(response)
            elif kind == 'new_session':
                await ws.send_json({'type': 'session_reset'})
        return ws

    async def http(request):
        if request.headers.get('Authorization') != 'Bearer fixture':
            return web.json_response({}, status=401)
        if request.method == 'POST':
            try:
                await request.json()
            except ValueError:
                return web.json_response({}, status=400)
        return web.json_response({'items': []})

    app = web.Application()
    app.router.add_get('/voice', socket)
    app.router.add_post('/turn', http)
    app.router.add_get('/pending', http)
    async with TestServer(app) as server:
        url = str(server.make_url('/voice')).replace('http:', 'ws:')
        good = await kit.check_server(url, 'fixture', http=str(server.make_url('')))
        assert good.ok, good.as_dict()
        duplicate = True
        bad = await kit.check_server(url, 'fixture')
        assert any(item['check'] == 'query yields exactly one matching response' and item['status'] == 'FAIL'
                   for item in bad.checks)


@pytest.mark.asyncio
@pytest.mark.parametrize(('query', 'authorization', 'accepted'), [
    ('', 'Bearer fixture', True), ('?token=wrong', 'bEaReR fixture', True),
    ('?token=fixture', 'Bearer wrong', False), ('?token=fixture', 'Basic wrong', True),
    ('?token=fixture', None, True), ('', 'Bearer wrong', False),
])
async def test_fake_server_bearer_auth_matches_adapter(query, authorization, accepted):
    app = kit.fake_app('fixture')
    headers = {'Authorization': authorization} if authorization else {}
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        async with session.ws_connect(server.make_url('/voice' + query), headers=headers) as ws:
            if not accepted:
                await ws.receive()
                assert ws.close_code == 4401
                return
            assert (await ws.receive_json())['type'] == 'welcome'
            await ws.send_json({'type': 'hello', 'protocol': 1,
                                'device': {'name': 'fixture', 'capabilities': {}}})
            await kit.receive(ws, 'hello_ack')
            await ws.send_json({'type': 'query', 'id': 'fixture', 'text': 'hello'})
            await kit.receive(ws, 'response')
            await ws.receive()
    assert app['reports'][0].ok


@pytest.mark.asyncio
async def test_connect_failure_has_own_stage_and_skips_later_checks():
    app = web.Application()  # /voice returns 404 on both connection attempts.
    async with TestServer(app) as server:
        report = await kit.check_server(str(server.make_url('/voice')), 'fixture', full=True, timeout=0.1)
    checks = {item['check']: item['status'] for item in report.checks}
    assert checks['connect'] == 'FAIL'
    assert checks['welcome protocol_max >= 1'] == 'SKIP'
    assert checks['hello_ack protocol == 1'] == 'SKIP'
    assert checks['query yields exactly one matching response'] == 'SKIP'
    assert checks['new_session yields session_reset'] == 'SKIP'
    assert checks['reminder push within 150 seconds'] == 'SKIP'


@pytest.mark.asyncio
@pytest.mark.parametrize(('protocol', 'final', 'cron', 'failed_check'), [
    (None, True, True, 'hello_ack protocol == 1'),
    (2, True, True, 'hello_ack protocol == 1'),
    (1, None, True, 'query yields exactly one matching response'),
    (1, False, True, 'query yields exactly one matching response'),
    (1, True, False, 'reminder push within 150 seconds'),
    (1, True, True, None),
])
async def test_checker_protocol_final_and_cron_origin(protocol, final, cron, failed_check):
    async def peer(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if request.query.get('token') != 'fixture':
            await ws.close(code=4401)
            return ws
        await ws.send_json({'type': 'welcome', 'protocol_max': 1})
        async for msg in ws:
            try:
                frame = msg.json()
            except ValueError:
                continue
            if not isinstance(frame, dict):
                continue
            if frame.get('type') == 'hello':
                await ws.send_json({'type': 'hello_ack', 'protocol': protocol, 'user': 'fixture',
                                    'device': 'fixture', 'server': {'version': 'fixture', 'features': []}})
            elif frame.get('type') == 'ping':
                await ws.send_json({'type': 'pong'})
            elif frame.get('type') == 'new_session':
                await ws.send_json({'type': 'session_reset'})
            elif frame.get('type') == 'query':
                if frame['text'] == 'Say hello.':
                    await ws.send_json({'type': 'response', 'turn': frame['id'], 'text': 'hi', 'final': final})
                else:
                    await ws.send_json({'type': 'push', 'id': 'message', 'origin': 'message', 'text': 'unrelated'})
                    if cron:
                        await ws.send_json({'type': 'push', 'id': 'cron', 'origin': 'cron', 'text': 'stretch'})
                    await ws.close()
        return ws

    app = web.Application()
    app.router.add_get('/voice', peer)
    async with TestServer(app) as server:
        report = await kit.check_server(str(server.make_url('/voice')), 'fixture', full=True, timeout=0.2)
    if failed_check:
        assert any(item['check'] == failed_check and item['status'] == 'FAIL' for item in report.checks), report.as_dict()
    else:
        assert report.ok, report.as_dict()


@pytest.mark.parametrize('timeout', [None, '0.25'])
def test_checker_cli_timeout(monkeypatch, timeout):
    cli = importlib.import_module(kit.__name__ + '.__main__')
    check = AsyncMock(return_value=kit.Report())
    monkeypatch.setattr(cli, 'check_server', check)
    args = ['conformance', 'check-server', '--url', 'ws://localhost/voice', '--token', 'fixture']
    if timeout is not None:
        args += ['--timeout', timeout]
    monkeypatch.setattr(sys, 'argv', args)
    with pytest.raises(SystemExit) as exit:
        cli.main()
    assert exit.value.code == 0
    assert check.call_args.kwargs['timeout'] == (10 if timeout is None else float(timeout))
