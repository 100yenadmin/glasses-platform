"""A departing device's leftovers reach other settled devices without another incoming frame."""
import asyncio
import time

import pytest
from glasses_plugin.devices import Connection, Identity, Outbox
from glasses_plugin.http_api import HttpSocket, _finish
from glasses_plugin.tests.test_voice import device, drained


def pushes(conn):
    return [frame for frame in conn.ws.frames if frame['type'] in ('push', 'response')]


async def release(adapter, conn):
    conn.ws.closed = True
    adapter._release(conn)
    if conn.held_task:
        await asyncio.gather(conn.held_task, return_exceptions=True)
    await drained(adapter)


@pytest.mark.asyncio
@pytest.mark.parametrize('ack', [False, True])
async def test_unacked_push_reaches_another_settled_device_once(make_adapter, ack):
    adapter = make_adapter()
    first = await device(adapter, ack=True)
    sent = await adapter.send('alice', 'Reminder.', metadata={'origin': 'cron'})
    assert sent.message_id in first.unacked
    other = await device(adapter, 'phone', ack=ack)
    await release(adapter, first)
    assert [(f['id'], f['text']) for f in pushes(other)] == [(sent.message_id, 'Reminder.')]
    assert (sent.message_id in other.unacked) is ack
    # Releasing the same connection again cannot replay its cleared leftovers.
    adapter._release(first)
    await drained(adapter)
    assert len(pushes(other)) == 1
    assert adapter.outbox.drain('alice') == []
    assert bool(Outbox(path=adapter.outbox.path).drain('alice')) is ack


@pytest.mark.asyncio
async def test_held_v0_push_reaches_another_live_device(make_adapter):
    adapter = make_adapter()
    first = await device(adapter, version=0)
    first.asking['q1'] = time.monotonic()
    sent = await adapter.send('alice', 'Reminder.', metadata={'origin': 'cron'})
    assert [item[3] for item in first.held] == [sent.message_id]
    other = await device(adapter, 'phone')
    await release(adapter, first)
    assert [(f['id'], f['text']) for f in pushes(other)] == [(sent.message_id, 'Reminder.')]


@pytest.mark.asyncio
async def test_http_finish_redelivers_pending_push_after_unlocking(make_adapter):
    adapter = make_adapter()
    first = Connection(HttpSocket(), Identity('alice', 'ray'), protocol=1, slot='ray#http')
    await adapter.registry.add(first)
    sent = await adapter.send('alice', 'Reminder.', metadata={'origin': 'cron'})
    assert [item[3] for item in first.pending] == [sent.message_id]
    other = await device(adapter, 'phone')
    async with asyncio.timeout(1):
        assert await _finish(adapter, first) == []
        await drained(adapter)
    assert first.ws.closed and first not in adapter.registry.live('alice')
    assert [(f['id'], f['text']) for f in pushes(other)] == [(sent.message_id, 'Reminder.')]


@pytest.mark.asyncio
async def test_push_already_delivered_elsewhere_is_not_replayed(make_adapter):
    adapter = make_adapter()
    first = await device(adapter, version=0)
    first.asking['q1'] = time.monotonic()
    other = await device(adapter, 'phone')
    sent = await adapter.send('alice', 'Reminder.', metadata={'origin': 'cron'})
    assert first.held and sent.message_id in adapter._delivered['alice']
    await release(adapter, first)
    assert [f['id'] for f in pushes(other)] == [sent.message_id]
    assert adapter.outbox.drain('alice') == []
    assert Outbox(path=adapter.outbox.path).drain('alice') == []


@pytest.mark.asyncio
async def test_release_never_delivers_to_another_user(make_adapter):
    adapter = make_adapter()
    first = await device(adapter, ack=True)
    await adapter.send('alice', 'Reminder.', metadata={'origin': 'cron'})
    other = await device(adapter, 'phone')
    stranger = await device(adapter, 'phone', user='bob')
    await release(adapter, first)
    assert [f['text'] for f in pushes(other)] == ['Reminder.']
    assert pushes(stranger) == []


@pytest.mark.asyncio
async def test_no_other_live_device_leaves_push_in_durable_outbox(make_adapter):
    adapter = make_adapter()
    first = await device(adapter, ack=True)
    sent = await adapter.send('alice', 'Reminder.', metadata={'origin': 'cron'})
    await release(adapter, first)
    assert [item[3] for item in adapter.outbox.drain('alice')] == [sent.message_id]
    assert [item[3] for item in Outbox(path=adapter.outbox.path).drain('alice')] == [sent.message_id]


@pytest.mark.asyncio
async def test_unsettled_device_waits_for_its_own_flush(make_adapter):
    adapter = make_adapter()
    first = await device(adapter, ack=True)
    sent = await adapter.send('alice', 'Reminder.', metadata={'origin': 'cron'})
    other = await device(adapter, 'phone', version=0)
    other.flushed = False
    await release(adapter, first)
    assert pushes(other) == [] and not other.flushed
    other.protocol = 1
    await adapter._flush_outbox(other)
    assert [f['id'] for f in pushes(other)] == [sent.message_id]


@pytest.mark.asyncio
async def test_redelivery_respects_the_other_devices_v0_hold(make_adapter):
    adapter = make_adapter()
    first = await device(adapter, ack=True)
    sent = await adapter.send('alice', 'Reminder.', metadata={'origin': 'cron'})
    other = await device(adapter, 'phone', version=0)
    other.asking['q1'] = time.monotonic()
    first.ws.closed = True
    adapter._release(first)
    # Wait only for redelivery: a held push's background timer deliberately remains pending.
    async with asyncio.timeout(1):
        while not other.held:
            await asyncio.sleep(0)
    assert pushes(other) == []
    assert [item[3] for item in other.held] == [sent.message_id]
    timer = other.held_task
    await adapter._answered(other, 'q1')
    await asyncio.gather(timer, return_exceptions=True)
    assert [f['text'] for f in pushes(other)] == ['Reminder.']


@pytest.mark.asyncio
async def test_redelivery_to_http_slot_waits_for_its_answer_to_count_as_delivered(make_adapter):
    adapter = make_adapter()
    first = await device(adapter, ack=True)
    sent = await adapter.send('alice', 'Reminder.', metadata={'origin': 'cron'})
    other = Connection(HttpSocket(), Identity('alice', 'phone'), protocol=1, slot='phone#http', flushed=True)
    await adapter.registry.add(other)
    await release(adapter, first)
    assert [item[3] for item, _ in other.sent_items] == [sent.message_id]
    assert sent.message_id not in adapter._delivered.get('alice', ())
    assert [item[3] for item in Outbox(path=adapter.outbox.path).drain('alice')] == [sent.message_id]


# Review of #68: a push another live device already holds (unacked, held for a waiting v0 device, or in an HTTP
# answer) is never sent to it a second time by the redelivery.
import time as _time
from glasses_plugin.devices import Connection as _Connection, Identity as _Identity
from glasses_plugin.http_api import HttpSocket as _HttpSocket


def _push_ids(conn):
    return [f['id'] for f in conn.ws.frames if f['type'] in ('push', 'response')]


@pytest.mark.asyncio
async def test_two_ack_devices_never_get_a_push_twice(make_adapter):
    adapter = make_adapter()
    first = await device(adapter, ack=True)
    other = await device(adapter, 'phone', ack=True)
    sent = await adapter.send('alice', 'R.', metadata={'origin': 'cron'})
    first.ws.closed = True
    adapter._release(first)
    await drained(adapter)
    assert _push_ids(other) == [sent.message_id]
    assert sent.message_id in other.unacked  # still durable until the phone acks


@pytest.mark.asyncio
async def test_unsettled_leftover_is_not_resent_to_an_ack_device_that_has_it(make_adapter):
    adapter = make_adapter()
    first = await device(adapter)
    first.flushed = False
    other = await device(adapter, 'phone', ack=True)
    sent = await adapter.send('alice', 'R.', metadata={'origin': 'cron'})
    first.ws.closed = True
    adapter._release(first)
    await drained(adapter)
    assert _push_ids(other) == [sent.message_id]


@pytest.mark.asyncio
async def test_an_http_answer_never_carries_a_push_twice(make_adapter):
    adapter = make_adapter()
    first = await device(adapter, ack=True)
    other = _Connection(_HttpSocket(), _Identity('alice', 'phone'), protocol=1, slot='phone#http', flushed=True)
    await adapter.registry.add(other)
    await adapter.send('alice', 'R.', metadata={'origin': 'cron'})
    first.ws.closed = True
    adapter._release(first)
    await drained(adapter)
    assert len(other.sent_items) == 1


@pytest.mark.asyncio
async def test_a_waiting_v0_device_hears_a_held_push_once(make_adapter):
    adapter = make_adapter()
    first = await device(adapter, ack=True)
    other = await device(adapter, 'phone', version=0)
    other.asking['q1'] = _time.monotonic()
    sent = await adapter.send('alice', 'R.', metadata={'origin': 'cron'})
    assert [item[3] for item in other.held] == [sent.message_id]
    first.ws.closed = True
    adapter._release(first)
    for _ in range(20):  # let the redelivery run; drained() would also wait out the 120 s hold timer
        await asyncio.sleep(0)
    assert [item[3] for item in other.held] == [sent.message_id]
    timer = other.held_task
    await adapter._answered(other, 'q1')
    await asyncio.gather(timer, return_exceptions=True)
    assert [f.get('text') for f in other.ws.frames].count('R.') == 1


@pytest.mark.asyncio
async def test_pushes_an_http_answer_failed_to_carry_reach_a_live_device(make_adapter):
    from glasses_plugin.http_api import _requeue
    adapter = make_adapter()
    other = await device(adapter, 'phone')
    item = (time.time(), 'Stretch.', 'cron', 'm-http')
    slot = Connection(HttpSocket(), Identity('alice', 'watch'), protocol=1, slot='watch#http', flushed=True)
    slot.sent_items.append((item, {'type': 'push', 'id': 'm-http', 'text': 'Stretch.', 'origin': 'cron'}))
    _requeue(adapter, slot)  # the HTTP answer was not written; the request is already released
    await drained(adapter)
    assert [f['id'] for f in other.ws.frames if f['type'] == 'push'] == ['m-http']


@pytest.mark.asyncio
async def test_a_failed_catch_up_write_keeps_the_push_queued(make_adapter):
    adapter = make_adapter()
    first = await device(adapter, ack=True)
    sent = await adapter.send('alice', 'R.', metadata={'origin': 'cron'})
    other = await device(adapter, 'phone')
    async def broken(frame):
        return False
    other.send_json = broken  # the surviving device's socket fails during the catch-up
    await release(adapter, first)
    assert pushes(other) == []
    assert [item[3] for item in adapter.outbox.drain('alice')] == [sent.message_id]
    assert other.flushed is False  # its next frame flushes again
