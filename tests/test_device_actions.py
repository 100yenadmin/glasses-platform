"""Actions target live capable devices from the same glasses session, never an outbox."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from glasses_plugin import tools
from glasses_plugin.adapter import Turn
from glasses_plugin.tests.test_voice import device


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,cap,payload', [('display', 'display', {'text': 'hi', 'seconds': 2}),
                                             ('haptic', 'haptics', {'pattern': 'double'})])
async def test_asking_device_preferred_then_other_capable_devices(make_adapter, kind, cap, payload):
    adapter = make_adapter()
    a = await device(adapter, **{cap: True})
    b = await device(adapter, 'phone', **{cap: True})
    unsupported = await device(adapter, 'none')
    old = await device(adapter, 'v0', version=0, **{cap: True})
    bob = await device(adapter, 'bob', user='bob', **{cap: True})
    adapter._active_turn['alice'] = Turn(a, 'q1')
    assert await adapter.device_action('alice', kind, payload) == 1
    assert a.ws.frames == [{'type': kind, **payload}]
    assert not b.ws.frames
    a.capabilities.clear()
    assert await adapter.device_action('alice', kind, payload) == 1
    assert b.ws.frames == [{'type': kind, **payload}]
    assert not unsupported.ws.frames and not old.ws.frames and not bob.ws.frames
    assert adapter.outbox.drain('alice') == []


@pytest.mark.asyncio
async def test_idle_fanout_no_device_and_offline_asker(make_adapter):
    adapter = make_adapter()
    a = await device(adapter, display=True)
    b = await device(adapter, 'phone', display=True)
    assert await adapter.device_action('alice', 'display', {'text': 'hi'}) == 2
    adapter._active_turn['alice'] = Turn(a, 'q1')
    adapter._release(a)
    assert await adapter.device_action('alice', 'display', {'text': 'hi'}) == 1
    adapter._release(b)
    assert await adapter.device_action('alice', 'display', {'text': 'hi'}) == 0
    assert adapter.outbox.drain('alice') == []


@pytest.mark.asyncio
async def test_device_action_hands_off_from_tool_thread(make_adapter):
    adapter = make_adapter()
    conn = await device(adapter, haptics=True)
    gateway_loop = asyncio.get_running_loop()
    original = conn.ws.send_json
    async def send(frame):
        assert asyncio.get_running_loop() is gateway_loop
        await original(frame)
    conn.ws.send_json = send
    result = await asyncio.to_thread(lambda: asyncio.run(adapter.device_action('alice', 'haptic', {'pattern': 'tap'})))
    assert result == 1 and conn.ws.frames == [{'type': 'haptic', 'pattern': 'tap'}]


@pytest.mark.asyncio
@pytest.mark.parametrize('handler,args', [(tools.show_text, {'text': 'hi'}), (tools.buzz, {})])
@pytest.mark.parametrize('active', [False, True])
async def test_tools_guard_outside_glasses(monkeypatch, handler, args, active):
    fake = type('Active', (), {'device_action': AsyncMock()})() if active else None
    monkeypatch.setattr(tools.adapter, 'ACTIVE', fake)
    monkeypatch.setattr(tools, 'get_session_env', lambda key: 'telegram')
    assert (await handler(args)).startswith('No glasses session:')
    if fake:
        fake.device_action.assert_not_called()


@pytest.mark.asyncio
async def test_tools_pass_session_user_and_default_pattern(monkeypatch):
    fake = type('Active', (), {'device_action': AsyncMock(return_value=2)})()
    monkeypatch.setattr(tools.adapter, 'ACTIVE', fake)
    monkeypatch.setattr(tools, 'get_session_env', lambda key: 'glasses' if key.endswith('PLATFORM') else 'alice')
    assert await tools.show_text({'text': 'hi', 'seconds': 3}) == 'Sent to 2 device(s).'
    fake.device_action.assert_awaited_with('alice', 'display', {'text': 'hi', 'seconds': 3})
    await tools.buzz({})
    fake.device_action.assert_awaited_with('alice', 'haptic', {'pattern': 'tap'})


@pytest.mark.asyncio
@pytest.mark.parametrize('args', [{}, {'text': ''}, {'text': 1}, {'text': 'x' * 501},
                                  {'text': 'hi', 'seconds': -1}, {'text': 'hi', 'seconds': True},
                                  {'text': 'hi', 'seconds': float('nan')}])
async def test_show_text_rejects_invalid_input(monkeypatch, args):
    action = AsyncMock()
    monkeypatch.setattr(tools, '_device_action', action)
    await tools.show_text(args)
    action.assert_not_called()


@pytest.mark.asyncio
async def test_buzz_rejects_unknown_pattern(monkeypatch):
    action = AsyncMock()
    monkeypatch.setattr(tools, '_device_action', action)
    assert 'Pattern must' in await tools.buzz({'pattern': 'triple'})
    action.assert_not_called()


def test_new_tools_registered_with_same_toolset_and_check():
    import glasses_plugin
    registered = []
    class Context:
        def register_platform(self, **kw):
            pass
        def register_tool(self, **kw):
            registered.append(kw)
    glasses_plugin.register(Context())
    by_name = {t['name']: t for t in registered}
    for name in ['glasses_show_text', 'glasses_buzz']:
        assert by_name[name]['toolset'] == 'glasses'
        assert by_name[name]['check_fn'] is tools.available and by_name[name]['is_async'] is True
