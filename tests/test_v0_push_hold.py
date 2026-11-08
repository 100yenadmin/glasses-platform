"""v0 devices (the hermes-glasses iOS app) have no push frame: a push arrives as a `response`. While such a device
waits for the answer to its question, a cron or other push sent now would be taken as that answer. Pushes therefore
wait until the answer has gone out. Prompts a turn waits on (approvals, clarify questions) never wait."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from gateway.platforms.event import ProcessingOutcome
from glasses_plugin.tests.test_routing_fixes import opened, until


async def ask(ws, events, query_id, text='what is the time?'):
    await ws.send_json({'type': 'query', 'id': query_id, 'text': text})
    return await asyncio.wait_for(events.get(), 3)


async def answer(adapter, event, text):
    await adapter.send('alice', text, reply_to=event.message_id, metadata={'notify': True})
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)


async def responses(ws, count):
    return [(await until(ws, 'response'))['text'] for _ in range(count)]


async def quiet(ws):
    """Nothing else is queued on this socket: a ping is answered first."""
    await ws.send_json({'type': 'ping'})
    assert (await ws.receive_json())['type'] == 'pong'


@pytest.fixture
def hermes(server):
    adapter, client = server
    events = asyncio.Queue()
    adapter.handle_message = AsyncMock(side_effect=events.put)
    return adapter, client, events


@pytest.mark.asyncio
async def test_a_push_mid_turn_reaches_a_v0_device_after_its_answer(hermes):
    adapter, client, events = hermes
    from glasses_plugin.devices import parse_devices
    adapter._tokens = parse_devices('alice:ray=tok-alice-1,alice:phone=tok-alice-2', '')
    glasses = await opened(client, version=0)
    other = await opened(client, 'tok-alice-2', version=0)
    await quiet(other)  # settles the idle device's protocol (v0) before the push
    event = await ask(glasses, events, 'q1')
    await adapter.on_processing_start(event)
    await adapter.send('alice', 'Reminder: check your gate.', metadata={'job_id': 'j1'})
    assert await responses(other, 1) == ['Reminder: check your gate.']  # a device that is not waiting gets it now
    await quiet(glasses)  # held, not sent
    await answer(adapter, event, 'It is noon.')
    assert await responses(glasses, 2) == ['It is noon.', 'Reminder: check your gate.']
    await glasses.close(); await other.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('metadata', [{'is_approval_prompt': True}, {'glasses_prompt': 'clarify'}])
async def test_prompts_the_turn_waits_on_are_never_held(hermes, metadata):
    adapter, client, events = hermes
    glasses = await opened(client, version=0)
    event = await ask(glasses, events, 'q1')
    await adapter.on_processing_start(event)
    await adapter.send('alice', 'Allow the command? Reply /approve or /deny.', metadata=metadata)
    assert await responses(glasses, 1) == ['Allow the command? Reply /approve or /deny.']
    await answer(adapter, event, 'Done.')
    assert await responses(glasses, 1) == ['Done.']
    await glasses.close()


@pytest.mark.asyncio
async def test_a_v1_device_gets_the_push_at_once(hermes):
    adapter, client, events = hermes
    glasses = await opened(client, version=1)
    event = await ask(glasses, events, 'q1')
    await adapter.on_processing_start(event)
    await adapter.send('alice', 'Reminder: check your gate.', metadata={'job_id': 'j1'})
    assert (await until(glasses, 'push'))['text'] == 'Reminder: check your gate.'
    await answer(adapter, event, 'It is noon.')
    assert (await until(glasses, 'response'))['text'] == 'It is noon.'
    await glasses.close()


@pytest.mark.asyncio
async def test_an_outbox_push_does_not_pose_as_the_answer_to_the_first_question(hermes):
    adapter, client, events = hermes
    await adapter.send('alice', 'Reminder: check your gate.', metadata={'job_id': 'j1'})  # no device: outbox
    glasses = await opened(client, version=0)
    event = await ask(glasses, events, 'q1')  # the first frame flushes the outbox
    await adapter.on_processing_start(event)
    await quiet(glasses)
    await answer(adapter, event, 'It is noon.')
    assert await responses(glasses, 2) == ['It is noon.', 'Reminder: check your gate.']
    await glasses.close()


@pytest.mark.asyncio
async def test_a_turn_that_ends_without_an_answer_releases_the_push(hermes):
    adapter, client, events = hermes
    glasses = await opened(client, version=0)
    event = await ask(glasses, events, 'q1')
    await adapter.on_processing_start(event)
    await adapter.send('alice', 'Reminder: check your gate.', metadata={'job_id': 'j1'})
    await quiet(glasses)
    await adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)  # nothing to say
    assert await responses(glasses, 1) == ['Reminder: check your gate.']
    await glasses.close()


@pytest.mark.asyncio
async def test_an_unanswered_question_holds_pushes_only_up_to_the_limit(hermes):
    adapter, client, events = hermes
    adapter._hold_seconds = 0.3
    glasses = await opened(client, version=0)
    event = await ask(glasses, events, 'q1')
    await adapter.on_processing_start(event)
    await adapter.send('alice', 'Reminder: check your gate.', metadata={'job_id': 'j1'})
    await quiet(glasses)
    assert await responses(glasses, 1) == ['Reminder: check your gate.']  # the turn is still running
    await adapter.send('alice', 'Second reminder.', metadata={'job_id': 'j2'})  # the stale question no longer holds
    assert await responses(glasses, 1) == ['Second reminder.']
    await glasses.close()


@pytest.mark.asyncio
async def test_a_held_push_survives_a_disconnect(hermes):
    adapter, client, events = hermes
    glasses = await opened(client, version=0)
    event = await ask(glasses, events, 'q1')
    await adapter.on_processing_start(event)
    await adapter.send('alice', 'Reminder: check your gate.', metadata={'job_id': 'j1'})
    await quiet(glasses)
    await glasses.close()
    async with asyncio.timeout(3):
        while adapter.registry.live('alice'):
            await asyncio.sleep(0.02)
    again = await opened(client, version=1)
    assert (await until(again, 'push'))['text'] == 'Reminder: check your gate.'
    await again.close()


@pytest.mark.asyncio
async def test_a_push_follows_the_answer_even_while_the_chain_runs_on(hermes):
    adapter, client, events = hermes
    from glasses_plugin.devices import parse_devices
    adapter._tokens = parse_devices('alice:ray=tok-alice-1,alice:phone=tok-alice-2', '')
    glasses = await opened(client, version=0)
    phone = await opened(client, 'tok-alice-2', version=1)
    first = await ask(glasses, events, 'q1')
    await adapter.on_processing_start(first)
    await adapter.send('alice', 'Reminder: check your gate.', metadata={'job_id': 'j1'})
    await adapter.send('alice', 'It is noon.', reply_to='q1', metadata={'notify': True})
    second = await ask(phone, events, 'q2')
    await adapter.on_processing_start(second)  # a queued follow-up: q1 is answered now, the chain goes on
    assert await responses(glasses, 2) == ['It is noon.', 'Reminder: check your gate.']
    await answer(adapter, first, 'Second answer.')
    await glasses.close(); await phone.close()
