import asyncio
from dataclasses import fields
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from glasses_plugin import adapter, register, tools
from gateway.platform_registry import PlatformEntry


def test_registration_and_filtered_kwargs(monkeypatch):
    captured = {}
    ctx = SimpleNamespace(register_platform=lambda **kw: captured.update(platform=kw),
                          register_tool=lambda **kw: captured.update(tool=kw))
    register(ctx)
    p, t = captured['platform'], captured['tool']
    assert p['name'] == 'glasses' and p['label'] == 'Smart glasses'
    assert p['check_fn']() and p['cron_deliver_env_var'] == 'GLASSES_HOME_CHANNEL'
    assert p['max_message_length'] == 4000 and 'display_tier' not in p
    assert not p['validate_config'](SimpleNamespace(extra={}))
    assert p['validate_config'](SimpleNamespace(extra={'token': 'tok-a'}))
    assert t['name'] == 'glasses_capture_photo' and t['is_async'] and t['toolset'] == 'glasses'
    monkeypatch.setattr('glasses_plugin.fields', lambda cls: [SimpleNamespace(name='emoji')])
    register(ctx)
    assert 'emoji' in captured['platform'] and 'max_message_length' not in captured['platform']


@pytest.mark.asyncio
async def test_tool_contract(monkeypatch):
    monkeypatch.setattr(tools, 'get_session_env', lambda name: '')
    assert 'No glasses session' in await tools.capture_photo({})
    monkeypatch.setattr(tools, 'get_session_env', lambda name: 'glasses' if name.endswith('PLATFORM') else 'alice')
    fake = SimpleNamespace(capture_for_session=AsyncMock(return_value=(b'jpeg', 'image-cache/photo.jpg')))
    monkeypatch.setattr(adapter, 'ACTIVE', fake)
    assert tools.available()
    result = await tools.capture_photo({'reason': 'read label'})
    assert result['_multimodal'] is True
    assert result['content'][1]['image_url']['url'].startswith('data:image/jpeg;base64,')
    assert result['meta'] == {'size_bytes': 4, 'native_vision': True}
    assert 'image-cache/photo.jpg' in result['text_summary']
    fake.capture_for_session.return_value = None
    assert await tools.capture_photo({}) == 'The glasses did not return a photo.'


@pytest.mark.asyncio
async def test_tool_cross_thread_capture(server):
    active, client = server
    from glasses_plugin.tests.test_adapter import opened, until
    ws = await opened(client)
    task = asyncio.create_task(asyncio.to_thread(lambda: asyncio.run(active.capture_for_session('alice'))))
    request = await until(ws, 'capture_photo')
    import base64
    from glasses_plugin.tests.test_adapter import JPEG
    photo = JPEG.read_bytes()
    await ws.send_json({'type': 'photo', 'request': request['request'], 'data': base64.b64encode(photo).decode()})
    result = await asyncio.wait_for(task, 3)
    assert result[0] == photo
    await ws.close()
