"""Queue and out-of-band delivery regressions (cross-model review C3–C6), on fake sockets for determinism."""
import asyncio

import pytest
from gateway.platforms.event import ProcessingOutcome
from glasses_plugin.adapter import Turn
from glasses_plugin.devices import Connection, Identity


class FakeWS:
    def __init__(self, gate=None, close_after=None):
        self.closed, self.frames, self.gate, self.close_after = False, [], gate, close_after

    async def send_json(self, frame):
        if self.gate is not None and not self.frames:
            await self.gate.wait()
        self.frames.append(frame)
        if self.close_after is not None and len(self.frames) >= self.close_after:
            self.closed = True

    async def close(self, **kwargs):
        self.closed = True


async def device(adapter, name, ws=None, protocol=1, flushed=False):
    conn = Connection(ws or FakeWS(), Identity('alice', name), protocol=protocol)
    conn.flushed = flushed
    await adapter.registry.add(conn)
    return conn


def texts(conn):
    return [frame['text'] for frame in conn.ws.frames]


def turn_event(message_id, user='alice'):
    return type('Event', (), {'source': type('Source', (), {'chat_id': user})(), 'message_id': message_id})()


@pytest.mark.asyncio
async def test_reply_for_offline_asker_goes_out_of_band_not_as_their_turn(server):
    adapter, _ = server
    glasses = await device(adapter, 'ray', flushed=True)
    phone = await device(adapter, 'phone', flushed=True)
    adapter._active_turn['alice'] = Turn(glasses, 't1')
    glasses.ws.closed = True
    preview = await adapter.send('alice', 'The answ', metadata={'expect_edits': True})
    await adapter.edit_message('alice', preview.message_id, 'The answer.')
    assert phone.ws.frames == []  # no deltas or status for a turn the phone did not ask
    await adapter.on_processing_complete(turn_event('t1'), ProcessingOutcome.SUCCESS)
    assert [(f['type'], f['origin'], f['text']) for f in phone.ws.frames] == [('push', 'reply', 'The answer.')]
    assert adapter.outbox.drain('alice') == []


@pytest.mark.asyncio
async def test_queued_pushes_keep_order_against_a_concurrent_push(server):
    adapter, _ = server
    adapter.outbox.append('alice', 'old1')
    adapter.outbox.append('alice', 'old2')
    gate = asyncio.Event()
    conn = await device(adapter, 'ray', FakeWS(gate))
    flush = asyncio.create_task(adapter._flush_outbox(conn))
    await asyncio.sleep(0.01)  # the flush is now blocked inside its first write
    push = asyncio.create_task(adapter.send('alice', 'new', metadata={'origin': 'cron'}))
    await asyncio.sleep(0.01)
    gate.set()
    await flush
    await push
    assert texts(conn) == ['old1', 'old2', 'new']


@pytest.mark.asyncio
async def test_every_connected_device_gets_a_push_queued_before_its_protocol_settled(server):
    adapter, _ = server
    glasses = await device(adapter, 'ray', protocol=0)
    phone = await device(adapter, 'phone', protocol=0)
    await adapter.send('alice', 'hi', metadata={'origin': 'cron'})
    await adapter._flush_outbox(glasses)
    await adapter._flush_outbox(phone)
    assert texts(glasses) == ['hi'] and texts(phone) == ['hi']


@pytest.mark.asyncio
async def test_device_leaving_before_its_flush_keeps_its_pushes(server):
    adapter, _ = server
    conn = await device(adapter, 'ray', protocol=0)
    await adapter.send('alice', 'hi', metadata={'origin': 'cron'})
    adapter._release(conn)
    assert [item[1] for item in adapter.outbox.drain('alice')] == ['hi']


@pytest.mark.asyncio
async def test_flush_that_hits_a_closed_socket_requeues_the_rest_in_order(server):
    adapter, _ = server
    for text in ('one', 'two', 'three'):
        adapter.outbox.append('alice', text)
    conn = await device(adapter, 'ray', FakeWS(close_after=1))
    await adapter._flush_outbox(conn)
    assert texts(conn) == ['one']
    assert [item[1] for item in adapter.outbox.drain('alice')] == ['two', 'three']


@pytest.mark.asyncio
async def test_cancelled_flush_requeues_the_unsent_batch(server):
    adapter, _ = server
    adapter.outbox.append('alice', 'one')
    adapter.outbox.append('alice', 'two')
    conn = await device(adapter, 'ray', FakeWS(asyncio.Event()))  # the first write never completes
    flush = asyncio.create_task(adapter._flush_outbox(conn))
    await asyncio.sleep(0.01)
    flush.cancel()
    with pytest.raises(asyncio.CancelledError):
        await flush
    assert [item[1] for item in adapter.outbox.drain('alice')] == ['one', 'two']
