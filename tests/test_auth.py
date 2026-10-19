"""Auth requirements, written before the auth implementation."""
import hmac
from unittest.mock import patch

from aiohttp import WSMsgType
import pytest


def test_parse_whitespace_and_shorthand():
    from glasses_plugin.devices import parse_devices
    table = parse_devices(' alice:ray = tok-alice-1 , bob:g2=tok-bob-1 ', ' tok-owner-1 ')
    assert [(i.user, i.device) for i in table.values()] == [
        ('alice', 'ray'), ('bob', 'g2'), ('owner', 'default')]


@pytest.mark.parametrize('spec', ['bad=hidden', 'u:/bad=hidden', 'u:d=', 'u:d=tok,,u:e=other', ':d=hidden'])
def test_malformed_hides_token(spec):
    from glasses_plugin.devices import parse_devices
    with pytest.raises(ValueError) as error:
        parse_devices(spec, '')
    assert 'entry' in str(error.value)
    assert 'hidden' not in str(error.value)


def test_duplicate_hides_token():
    from glasses_plugin.devices import parse_devices
    with pytest.raises(ValueError) as error:
        parse_devices('alice:a=tok-secret,bob:b=tok-secret', '')
    assert 'tok-secret' not in str(error.value)


def test_auth_compares_every_entry():
    from glasses_plugin.devices import authenticate, parse_devices
    table = parse_devices('alice:a=tok-a,bob:b=tok-b,carol:c=tok-c', '')
    with patch('glasses_plugin.devices.hmac.compare_digest', wraps=hmac.compare_digest) as compare:
        assert authenticate('tok-a', table).user == 'alice'
        assert compare.call_count == 3
        assert all(isinstance(c.args[0], bytes) for c in compare.call_args_list)
    assert authenticate('wrong', table) is None
    assert authenticate(None, table) is None
    assert authenticate('', table) is None
    assert authenticate('tok-a', {}) is None
    assert authenticate('日本語', table) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('token', [None, 'wrong', ''])
async def test_reject_upgrades_then_closes(server, token):
    adapter, client = server
    kwargs = {} if token is None else {'params': {'token': token}}
    async with client.ws_connect('/voice', **kwargs) as ws:
        msg = await ws.receive(timeout=2)
        assert msg.type == WSMsgType.CLOSE
        assert msg.data == 4401


@pytest.mark.asyncio
@pytest.mark.parametrize('bearer', [False, True])
async def test_accept_query_and_bearer(server, bearer):
    _, client = server
    kw = {'headers': {'Authorization': 'Bearer tok-alice-1'}} if bearer else {'params': {'token': 'tok-alice-1'}}
    async with client.ws_connect('/voice', **kw) as ws:
        assert await ws.receive_json(timeout=2) == {'type': 'welcome', 'protocol_max': 1}


@pytest.mark.asyncio
async def test_empty_config_refuses_start(make_adapter, caplog):
    adapter = make_adapter(devices='', token='')
    assert await adapter.connect() is False
    assert 'no device tokens configured; refusing to start' in caplog.text


@pytest.mark.asyncio
async def test_empty_auth_table_rejects(server):
    adapter, client = server
    adapter._tokens = {}
    async with client.ws_connect('/voice', params={'token': 'tok-alice-1'}) as ws:
        assert (await ws.receive(timeout=2)).data == 4401
