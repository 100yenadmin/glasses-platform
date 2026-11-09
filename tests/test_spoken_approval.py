"""A dangerous-command approval is said as a yes/no question, and a spoken yes or no answers it."""
import asyncio
from dataclasses import fields
from unittest.mock import AsyncMock

import pytest
from gateway.platforms.base import ExecApprovalPrompt
from glasses_plugin.adapter import GlassesAdapter, spoken_choice
from glasses_plugin.devices import parse_devices
from glasses_plugin.tests.test_routing_fixes import opened, until

ACTIONS = [('Allow once', 'once', 'primary'), ('Allow for session', 'session', ''),
           ('Always allow', 'always', ''), ('Deny', 'deny', 'danger')]


HAS_ID = 'request_id' in {f.name for f in fields(ExecApprovalPrompt)}  # Hermes 0.23 and later


def prompt(user='alice', request_id='r1', actions=ACTIONS, command='rm -rf build/'):
    extra = {'request_id': request_id} if HAS_ID else {}
    return ExecApprovalPrompt(chat_id=user, session_key=f'agent:main:glasses:dm:{user}', text='(card text)',
                              actions=actions, command=command, description='Deletes files.',
                              smart_denied=False, **extra)


def rid(request_id):
    return request_id if HAS_ID else None


@pytest.fixture
def hermes(server, monkeypatch):
    adapter, client = server
    adapter._tokens = parse_devices('alice:ray=tok-alice-1,alice:phone=tok-alice-2,bob:g2=tok-bob-1', '')
    adapter.handle_message = AsyncMock()
    calls = []

    def resolve(session_key, choice, resolve_all=False, reason=None, request_id=None):
        calls.append((session_key, choice, request_id))
        return resolve.result
    resolve.result = 1
    monkeypatch.setattr('tools.approval.resolve_gateway_approval', resolve)
    return adapter, client, calls, resolve


async def say(ws, text, query_id='q1'):
    await ws.send_json({'type': 'query', 'id': query_id, 'text': text})


@pytest.mark.parametrize('text,choice', [
    ('Yes.', 'once'), ('yeah', 'once'), ('Hermes, go ahead!', 'once'), ('OK', 'once'), ('Allow it', 'once'),
    ('No', 'deny'), ("Don't do it.", 'deny'), ('don’t', 'deny'), ('Hey Hermes, stop', 'deny'), ('cancel', 'deny'),
    ('yes, and also delete the cache', None), ('always allow', None), ('what does it do?', None), ('', None)])
def test_only_a_whole_yes_or_no_counts(text, choice):
    assert spoken_choice(text) == choice


def test_the_adapter_renders_native_approvals():
    assert GlassesAdapter.supports_exec_approval_buttons()


@pytest.mark.asyncio
async def test_the_prompt_is_a_short_question_on_every_device_even_a_waiting_v0_one(hermes):
    adapter, client, calls, _ = hermes
    ray = await opened(client, version=1)
    phone = await opened(client, 'tok-alice-2', version=0)
    await say(phone, 'clean the build folder')  # the v0 phone now waits for its answer
    await asyncio.sleep(0.05)
    result = await adapter._send_exec_approval_prompt(prompt())
    assert result.success
    pushed = await until(ray, 'push')
    assert pushed['origin'] == 'approval'
    assert pushed['text'].startswith('Hermes wants to run: rm -rf build/. Deletes files. Say yes to allow it once')
    assert '**' not in pushed['text'] and '⚠' not in pushed['text']
    assert (await until(phone, 'response'))['text'] == pushed['text']  # not held behind the phone's question
    await ray.close(); await phone.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('words,choice,reply', [('Yes, go ahead', None, None), ('yes', 'once', 'Okay, going ahead.'),
                                                ('No.', 'deny', "Okay, I won't run it.")])
async def test_a_spoken_yes_or_no_answers_that_request(hermes, words, choice, reply):
    adapter, client, calls, _ = hermes
    ray = await opened(client, version=1)
    await adapter._send_exec_approval_prompt(prompt())
    await until(ray, 'push')
    await say(ray, words)
    if choice is None:  # not a whole yes: the agent hears it and the approval still waits
        await asyncio.sleep(0.1)
        assert calls == [] and adapter.handle_message.await_count == 1 and adapter._approvals['alice']
    else:
        frame = await until(ray, 'response')
        assert (frame['text'], frame['turn']) == (reply, 'q1')
        assert calls == [('agent:main:glasses:dm:alice', choice, rid('r1'))]
        assert adapter.handle_message.await_count == 0 and 'alice' not in adapter._approvals
    await ray.close()


@pytest.mark.asyncio
async def test_yes_goes_to_the_agent_when_nothing_waits(hermes):
    adapter, client, calls, resolve = hermes
    ray = await opened(client, version=1)
    await say(ray, 'yes', 'q0')
    await asyncio.sleep(0.1)
    assert calls == [] and adapter.handle_message.await_count == 1
    await adapter._send_exec_approval_prompt(prompt())
    await until(ray, 'push')
    resolve.result = 0  # answered elsewhere (a typed /approve) or timed out
    await say(ray, 'yes')
    await asyncio.sleep(0.1)
    assert len(calls) == 1 and adapter.handle_message.await_count == 2 and 'alice' not in adapter._approvals
    await ray.close()


@pytest.mark.asyncio
async def test_another_users_yes_never_answers_it(hermes):
    adapter, client, calls, _ = hermes
    await adapter._send_exec_approval_prompt(prompt('alice'))
    bob = await opened(client, 'tok-bob-1', version=1)
    await say(bob, 'yes')
    await asyncio.sleep(0.1)
    assert calls == [] and adapter.handle_message.await_count == 1 and adapter._approvals['alice']
    await bob.close()


@pytest.mark.asyncio
async def test_yes_never_approves_more_than_offered(hermes):
    adapter, client, calls, _ = hermes
    ray = await opened(client, version=1)
    await adapter._send_exec_approval_prompt(prompt(actions=[('Deny', 'deny', 'danger')]))
    assert 'Say no to block it.' in (await until(ray, 'push'))['text']
    await say(ray, 'yes')
    await asyncio.sleep(0.1)
    assert calls == [] and adapter.handle_message.await_count == 1
    await say(ray, 'no', 'q2')
    assert (await until(ray, 'response'))['turn'] == 'q2' and calls[0][1] == 'deny'
    await ray.close()


@pytest.mark.asyncio
async def test_two_waiting_approvals_are_answered_oldest_first(hermes):
    adapter, client, calls, _ = hermes
    ray = await opened(client, version=1)
    await adapter._send_exec_approval_prompt(prompt(request_id='r1'))
    await adapter._send_exec_approval_prompt(prompt(request_id='r2', command='git push --force'))
    await say(ray, 'yes', 'q1')
    await say(ray, 'no', 'q2')
    await until(ray, 'response'); await until(ray, 'response')
    assert [(c[1], c[2]) for c in calls] == [('once', rid('r1')), ('deny', rid('r2'))]
    await ray.close()


@pytest.mark.asyncio
async def test_a_prompt_for_an_unknown_user_falls_back_to_hermes_text(hermes):
    adapter, *_ = hermes
    assert not (await adapter._send_exec_approval_prompt(prompt('mallory'))).success
