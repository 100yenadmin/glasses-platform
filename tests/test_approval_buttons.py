"""Button and spoken approvals resolve only the requesting user's live Hermes request."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from glasses_plugin.devices import parse_devices
from glasses_plugin.tests.test_spoken_approval import prompt, hermes, ask_approval
from glasses_plugin.tests.test_routing_fixes import opened, until
from glasses_plugin.tests.test_voice import device, drained, recording, speech


@pytest.fixture
def approvals(make_adapter, monkeypatch):
    adapter = make_adapter()
    adapter._tokens = parse_devices('alice:ray=a,bob:g2=b')
    queues, calls = {}, []
    def resolve(key, choice, request_id=None):
        calls.append((key, choice, request_id))
        if request_id in queues.get(key, []):
            queues[key].remove(request_id)
            return 1
        return 0
    monkeypatch.setattr('tools.approval.list_gateway_approvals',
                        lambda key: [{'request_id': id} for id in queues.get(key, [])])
    monkeypatch.setattr('tools.approval.resolve_gateway_approval', resolve)
    monkeypatch.setattr('tools.clarify_gateway.has_pending', lambda key: False)
    async def ask(user='alice', id='r1', **kw):
        item = prompt(user=user, request_id=id, **kw)
        queues.setdefault(item.session_key, []).append(id)
        assert (await adapter._send_exec_approval_prompt(item)).success
        return adapter._approvals[user][-1]
    return adapter, queues, calls, ask


@pytest.mark.asyncio
async def test_buttons_get_request_others_get_same_text_push(approvals):
    adapter, _, _, ask = approvals
    button = await device(adapter, buttons=True)
    plain = await device(adapter, 'phone')
    legacy = await device(adapter, 'old', version=0, buttons=True)
    approval = await ask()
    request = button.ws.frames[0]
    assert request['type'] == 'approval_request' and request['id'] == 'r1'
    assert request['command'] == 'rm -rf build/' and request['description'] == 'Deletes files.'
    assert request['choices'] == ['once', 'session', 'always', 'deny']
    assert plain.ws.frames[0]['type'] == 'push' and plain.ws.frames[0]['origin'] == 'approval'
    assert request['text'] == plain.ws.frames[0]['text'] == legacy.ws.frames[0]['text']
    assert approval.connections == {button}


@pytest.mark.asyncio
@pytest.mark.parametrize('choice', ['once', 'session', 'always', 'deny'])
async def test_allowed_choices_resolve_and_close_only_other_recipients(approvals, choice):
    adapter, _, calls, ask = approvals
    a = await device(adapter, buttons=True)
    b = await device(adapter, 'phone', buttons=True)
    plain = await device(adapter, 'plain')
    bob = await device(adapter, 'bob', user='bob', buttons=True)
    await ask()
    await adapter._button_approval(a, {'id': 'r1', 'choice': choice})
    result = a.ws.frames[-1]
    assert result == {'type': 'approval_result', 'id': 'r1', 'ok': True, 'choice': choice,
                      'message': "Okay, I won't run it." if choice == 'deny' else 'Okay, going ahead.'}
    assert calls == [('agent:main:glasses:dm:alice', choice, 'r1')]
    assert b.ws.frames[-1] == {'type': 'approval_closed', 'id': 'r1', 'choice': choice}
    assert not any(f['type'] == 'approval_closed' for f in a.ws.frames + plain.ws.frames + bob.ws.frames)
    assert 'alice' not in adapter._approvals


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['unknown', 'other_user', 'expired', 'not_offered', 'non_string', 'non_string_id'])
async def test_invalid_buttons_resolve_nothing(approvals, bad):
    adapter, queues, calls, ask = approvals
    conn = await device(adapter, buttons=True)
    await ask('bob' if bad == 'other_user' else 'alice', actions=[('Once', 'once', ''), ('Deny', 'deny', '')])
    if bad == 'expired':
        queues['agent:main:glasses:dm:alice'].clear()
    request = {'id': 'missing' if bad == 'unknown' else ['r1'] if bad == 'non_string_id' else 'r1',
               'choice': [] if bad == 'non_string' else 'always' if bad == 'not_offered' else 'once'}
    await adapter._button_approval(conn, request)
    await drained(adapter)
    result = next(f for f in conn.ws.frames if f['type'] == 'approval_result')
    assert result['ok'] is False and result['message'] and calls == []
    if bad not in ('expired', 'unknown', 'other_user'):
        assert queues['agent:main:glasses:dm:alice'] == ['r1']


@pytest.mark.asyncio
async def test_resolution_race_does_not_claim_success(approvals, monkeypatch):
    adapter, _, _, ask = approvals
    conn = await device(adapter, buttons=True)
    await ask()
    monkeypatch.setattr('tools.approval.resolve_gateway_approval', lambda *a, **kw: 0)
    await adapter._button_approval(conn, {'id': 'r1', 'choice': 'once'})
    assert conn.ws.frames[-1]['ok'] is False


@pytest.mark.asyncio
async def test_spoken_audio_yes_uses_same_approval_and_closes_buttons(approvals, speech):
    adapter, _, calls, ask = approvals
    speaker = await device(adapter)
    button = await device(adapter, 'phone', buttons=True)
    await ask()
    speech[0].return_value = {'success': True, 'transcript': 'yes'}
    adapter.handle_message = AsyncMock()
    await adapter._process_query(speaker, {'id': 'q1', 'audio': recording()})
    assert calls == [('agent:main:glasses:dm:alice', 'once', 'r1')]
    assert [f['type'] for f in speaker.ws.frames] == ['push', 'transcript', 'response']
    assert button.ws.frames[-1] == {'type': 'approval_closed', 'id': 'r1', 'choice': 'once'}
    adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_typed_resolution_dismisses_button_prompt(approvals):
    adapter, queues, _, ask = approvals
    conn = await device(adapter, buttons=True)
    await ask()
    async def handle(event):
        queues['agent:main:glasses:dm:alice'].clear()
    adapter.handle_message = handle
    await adapter._process_query(conn, {'id': 'q1', 'text': '/approve'})
    await drained(adapter)
    assert conn.ws.frames[-1] == {'type': 'approval_closed', 'id': 'r1'}


@pytest.mark.asyncio
async def test_button_prompt_has_audio_when_declared(approvals, speech):
    adapter, _, _, ask = approvals
    conn = await device(adapter, buttons=True, audio_out=['audio/mpeg'])
    await ask()
    await drained(adapter)
    assert [f['type'] for f in conn.ws.frames] == ['approval_request', 'audio']
    assert conn.ws.frames[1]['ref'] == conn.ws.frames[0]['id'] == 'r1'  # the device can match audio to the card
    speech[1].assert_called_once_with(conn.ws.frames[0]['text'])


@pytest.mark.asyncio
async def test_offline_and_unsettled_prompts_keep_text_push(approvals):
    adapter, _, _, ask = approvals
    await ask()
    conn = await device(adapter, buttons=True)
    conn.flushed = False
    await adapter._flush_outbox(conn)
    assert conn.ws.frames[0]['type'] == 'push'
    conn.flushed = False
    await ask(id='r2')
    await adapter._flush_outbox(conn)
    assert [f['type'] for f in conn.ws.frames] == ['push', 'push']
    assert not adapter._approvals['alice'][-1].connections


@pytest.mark.asyncio
async def test_websocket_button_dispatch_is_v1_only(hermes):
    adapter, client, calls, resolve = hermes
    ws = await opened(client, version=1)
    await ws.send_json({'type': 'hello', 'device': {'capabilities': {'buttons': True}}})
    await until(ws, 'hello_ack')
    await ask_approval(adapter, resolve)
    await until(ws, 'approval_request')
    legacy = await opened(client, 'tok-alice-2', version=0)
    await legacy.send_json({'type': 'approval_response', 'id': 'r1', 'choice': 'deny'})
    await legacy.send_json({'type': 'ping'})
    await until(legacy, 'pong')
    assert calls == []
    await ws.send_json({'type': 'approval_response', 'id': 'r1', 'choice': 'once'})
    assert (await until(ws, 'approval_result'))['ok'] is True
    assert calls[0][2] == 'r1'
    await ws.close()
    await legacy.close()


@pytest.mark.asyncio
async def test_a_button_device_that_answers_by_voice_also_closes_its_card(approvals, speech):
    adapter, _, calls, ask = approvals
    conn = await device(adapter, buttons=True)
    await ask()
    speech[0].return_value = {'success': True, 'transcript': 'yes'}
    adapter.handle_message = AsyncMock()
    await adapter._process_query(conn, {'id': 'q1', 'audio': recording()})
    assert calls == [('agent:main:glasses:dm:alice', 'once', 'r1')]
    assert {'type': 'approval_closed', 'id': 'r1', 'choice': 'once'} in conn.ws.frames
