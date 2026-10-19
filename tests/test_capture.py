import asyncio
import base64

import pytest
from glasses_plugin.capture import PhotoBroker, decode_photo, should_capture_photo
from glasses_plugin.tests.test_devices import connection


@pytest.mark.asyncio
async def test_broker_disconnect_error_timeout_and_matching():
    broker = PhotoBroker()
    conn, other = connection(), connection(device='other')
    task = asyncio.create_task(broker.request(conn, 'tool', 1))
    await asyncio.sleep(0)
    request = conn.ws.send_json.call_args.args[0]['request']
    broker.resolve(other, {'type': 'photo', 'request': request, 'data': 'aGk='})
    assert not task.done()
    broker.cancel_all(conn)
    assert await asyncio.wait_for(task, 1) is None
    assert not broker._pending
    task = asyncio.create_task(broker.request(conn, 'tool', 1))
    await asyncio.sleep(0)
    request = conn.ws.send_json.call_args.args[0]['request']
    broker.resolve(conn, {'type': 'photo_error', 'request': request})
    assert await task is None
    assert await broker.request(conn, 'tool', 0.001) is None
    assert not broker._pending


def test_decode_and_heuristic():
    assert decode_photo('aGk=', 2) == b'hi'
    assert decode_photo('aGk=', 1) is None
    for value in ('bad!', '', None, ['x']):
        assert decode_photo(value, 100) is None
    assert should_capture_photo('what am I looking at?', 100, 101)
    assert should_capture_photo('this bottle', 0, 121)
    assert not should_capture_photo('this bottle', 100, 101)
    assert not should_capture_photo('this morning', 0, 1000)
