"""Pushes waiting for an offline device survive a gateway restart."""
import asyncio
import json
import os
import stat

import pytest
from glasses_plugin.devices import Outbox
from glasses_plugin.tests.test_routing_fixes import opened, until


def test_the_queue_is_written_and_read_back(tmp_path):
    path = tmp_path / 'glasses' / 'outbox.json'
    box = Outbox(path=path)
    box.append('alice', 'Reminder: gate 42.', 'cron', 'm1')
    box.append('bob', 'Hi Bob.', 'message', 'm2')
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    again = Outbox(path=path)
    assert [i[1:] for i in again.drain('alice')] == [('Reminder: gate 42.', 'cron', 'm1')]
    assert [i[3] for i in Outbox(path=path).drain('alice')] == ['m1']  # taken but not sent: a crash keeps it
    again.sent('m1')
    assert Outbox(path=path).drain('alice') == [] and [i[3] for i in Outbox(path=path).drain('bob')] == ['m2']


def test_held_and_pending_pushes_stay_in_the_file_until_sent_or_forgotten(tmp_path):
    path = tmp_path / 'outbox.json'
    box = Outbox(path=path)
    box.track('alice', (1e12, 'held for a v0 device', 'cron', 'h1'))
    box.track('alice', (1e12, 'pending a flush', 'cron', 'p1'))
    assert sorted(i[3] for i in Outbox(path=path).drain('alice')) == ['h1', 'p1']
    box.sent('h1')
    box.forget(['p1'])  # another device already got it
    assert Outbox(path=path).drain('alice') == []
    box.track('alice', (1e12, 'x', 'cron', 'x1'))
    box.merge('alice', [(1e12, 'x', 'cron', 'x1')])  # back in the queue: one copy
    assert [i[3] for i in Outbox(path=path).drain('alice')] == ['x1']


def test_expired_items_are_dropped_at_load_and_the_bound_holds(tmp_path):
    path = tmp_path / 'outbox.json'
    now = [1000.0]
    box = Outbox(maxlen=2, ttl_hours=1, clock=lambda: now[0], path=path)
    for n in range(3):
        box.append('alice', f'm{n}', 'message', f'id{n}')
    assert [i[3] for i in Outbox(maxlen=2, ttl_hours=1, clock=lambda: now[0], path=path).drain('alice')] == ['id1', 'id2']
    box = Outbox(maxlen=2, ttl_hours=1, clock=lambda: now[0], path=path)
    box.append('alice', 'old', 'message', 'old')
    now[0] += 3601
    assert Outbox(maxlen=2, ttl_hours=1, clock=lambda: now[0], path=path).drain('alice') == []


@pytest.mark.parametrize('content', ['not json', '[1, 2]', '{"alice": [["x"]]}'])
def test_an_unreadable_file_is_ignored_not_fatal(tmp_path, content):
    path = tmp_path / 'outbox.json'
    path.write_text(content)
    box = Outbox(path=path)
    assert box.drain('alice') == []
    box.append('alice', 'fresh', 'message', 'f1')
    assert [i[3] for i in Outbox(path=path).drain('alice')] == ['f1']


def test_without_a_path_nothing_is_written(tmp_path):
    Outbox().append('alice', 'x')
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_a_reminder_queued_before_a_restart_reaches_the_device_after_it(make_adapter):
    from aiohttp.test_utils import TestClient, TestServer
    first = make_adapter()
    await first.connect()
    await first.send('alice', 'Time to stretch your legs!', metadata={'job_id': 'j1'})  # no device connected
    await first.disconnect()
    second = make_adapter()  # the gateway restarted
    await second.connect()
    client = TestClient(TestServer(second._app))
    await client.start_server()
    try:
        ws = await opened(client, version=1)
        frame = await until(ws, 'push')
        assert (frame['text'], frame['origin']) == ('Time to stretch your legs!', 'cron')
        await ws.close()
    finally:
        await second.disconnect()
        await client.close()
    third = make_adapter()
    assert third.outbox.drain('alice') == []  # delivered once, not again after the next restart


@pytest.mark.asyncio
async def test_a_push_held_for_a_waiting_v0_device_survives_a_crash(make_adapter):
    from aiohttp.test_utils import TestClient, TestServer
    adapter = make_adapter()
    await adapter.connect()
    client = TestClient(TestServer(adapter._app))
    await client.start_server()
    try:
        ws = await opened(client, version=0)
        await ws.send_json({'type': 'query', 'id': 'q1', 'text': 'what time is it?'})  # the device now waits
        await asyncio.sleep(0.05)
        await adapter.send('alice', 'Reminder: gate 42.', metadata={'job_id': 'j1'})  # held behind that answer
        assert [i[1] for i in make_adapter().outbox.drain('alice')] == ['Reminder: gate 42.']  # a crash now keeps it
        await ws.close()
    finally:
        await adapter.disconnect()
        await client.close()


def test_persistence_can_be_turned_off(make_adapter):
    adapter = make_adapter(outbox_persist='off')
    assert adapter.outbox.path is None
    assert make_adapter().outbox.path.name == 'outbox.json'
