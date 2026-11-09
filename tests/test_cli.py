"""`hermes glasses pair | devices | revoke` against the profile's real .env and config.yaml helpers."""
import argparse
import os
from types import SimpleNamespace

import pytest
from glasses_plugin import cli, register
from glasses_plugin.devices import authenticate, parse_devices


def run(*argv):
    parser = argparse.ArgumentParser()
    cli.setup(parser)
    return cli.main(parser.parse_args(list(argv)))


def env_file():
    path = os.path.join(os.environ['HERMES_HOME'], '.env')
    return open(path).read() if os.path.exists(path) else ''


def stored():
    from hermes_cli.config import get_env_value
    return parse_devices(get_env_value('GLASSES_DEVICES') or '')


def url_token(output):
    line = next(l for l in output.splitlines() if 'token=' in l)
    return line.strip().split('token=', 1)[1]


@pytest.fixture
def home(monkeypatch):
    os.makedirs(os.environ['HERMES_HOME'], exist_ok=True)
    monkeypatch.setattr(cli, '_lan_address', lambda: '192.168.1.20')

    def config(text):
        with open(os.path.join(os.environ['HERMES_HOME'], 'config.yaml'), 'w') as f:
            f.write(text)
    return config


def test_pair_stores_a_working_token_and_prints_its_url(home, capsys):
    assert run('pair', 'alice', 'rayban', '--no-qr') == 0
    out = capsys.readouterr().out
    token = url_token(out)
    assert 'ws://192.168.1.20:8765/voice?token=' in out
    assert authenticate(token, stored()).device == 'rayban'
    assert len(token) >= 40 and 'hermes gateway restart' in out


def test_pair_keeps_devices_from_config_yaml(home, capsys):
    home('platforms:\n  glasses:\n    extra:\n      devices: "alice:old=tok-from-yaml"\n      port: 9001\n')
    assert run('pair', 'alice', 'phone', '--no-qr') == 0
    assert ':9001/voice?token=' in capsys.readouterr().out
    assert sorted(i.device for i in stored().values()) == ['old', 'phone']  # .env wins over YAML, so YAML's entry is carried


def test_pairing_twice_needs_replace_and_replace_retires_the_old_token(home, capsys):
    run('pair', 'alice', 'rayban', '--no-qr')
    first = url_token(capsys.readouterr().out)
    assert run('pair', 'alice', 'rayban', '--no-qr') == 1
    assert 'already paired' in capsys.readouterr().out
    assert run('pair', 'alice', 'rayban', '--no-qr', '--replace') == 0
    second = url_token(capsys.readouterr().out)
    table = stored()
    assert second != first and authenticate(first, table) is None and len(table) == 1


def test_devices_never_prints_tokens_and_revoke_removes_one(home, capsys):
    run('pair', 'alice', 'rayban', '--no-qr')
    run('pair', 'alice', 'phone', '--no-qr')
    capsys.readouterr()
    assert run('devices') == 0
    out = capsys.readouterr().out
    assert out.split() == ['alice:rayban', 'alice:phone']
    assert run('revoke', 'alice', 'rayban') == 0
    assert [i.device for i in stored().values()] == ['phone']
    assert run('revoke', 'alice', 'rayban') == 1
    run('revoke', 'alice', 'phone')
    assert 'GLASSES_DEVICES' not in env_file()


def test_explicit_host_and_wss(home, capsys):
    run('pair', 'bob', 'g2', '--host', 'mac.tailnet.example', '--port', '443', '--wss', '--no-qr')
    assert 'wss://mac.tailnet.example:443/voice?token=' in capsys.readouterr().out


def test_bad_names_and_a_malformed_list_write_nothing(home, capsys):
    assert run('pair', 'al ice', 'x', '--no-qr') == 2
    home('platforms:\n  glasses:\n    extra:\n      devices: "no-colon=tok"\n')
    assert run('pair', 'alice', 'x', '--no-qr') == 1
    assert 'not valid' in capsys.readouterr().out and env_file() == ''


def test_qr_code_is_printed_when_available(home, capsys):
    pytest.importorskip('qrcode')
    run('pair', 'alice', 'rayban')
    assert '█' in capsys.readouterr().out


def test_pair_says_why_when_no_qr_code_can_be_drawn(home, capsys, monkeypatch):
    """#47: 0.5.0 hid a missing qrcode package; pair now says so instead of silently printing only the URL."""
    monkeypatch.setattr(cli, '_qr', lambda url: None)
    run('pair', 'alice', 'rayban')
    out = capsys.readouterr().out
    assert 'No QR code: the qrcode package is missing' in out and 'ws://' in out
    run('pair', 'alice', 'watch', '--no-qr')
    assert 'No QR code' not in capsys.readouterr().out


def test_register_adds_the_cli_command_when_hermes_supports_it():
    seen = {}
    ctx = SimpleNamespace(register_platform=lambda **kw: None, register_tool=lambda **kw: None,
                          register_cli_command=lambda name, help, setup, main, description='': seen.update(name=name))
    register(ctx)
    assert seen == {'name': 'glasses'}


def test_revoking_the_last_device_never_brings_back_yaml_devices(home, capsys):
    home('platforms:\n  glasses:\n    extra:\n      devices: "alice:old=tok-from-yaml"\n')
    assert run('revoke', 'alice', 'old') == 1  # GLASSES_DEVICES would be empty, and YAML's list applies again
    assert 'last device' in capsys.readouterr().out and env_file() == ''
    run('pair', 'alice', 'new', '--no-qr')
    assert run('revoke', 'alice', 'old') == 0
    assert 'config.yaml still lists it' in capsys.readouterr().out
    assert [i.device for i in stored().values()] == ['new']


def test_pair_prints_no_url_when_the_env_write_is_refused(home, capsys, monkeypatch):
    monkeypatch.setattr('hermes_cli.config.save_env_value', lambda key, value: None)  # a managed install's refusal
    assert run('pair', 'alice', 'rayban', '--no-qr') == 1
    out = capsys.readouterr().out
    assert 'token=' not in out and 'Nothing was paired' in out


def test_revoke_reports_a_refused_env_write(home, capsys, monkeypatch):
    run('pair', 'alice', 'rayban', '--no-qr')
    run('pair', 'alice', 'phone', '--no-qr')
    monkeypatch.setattr('hermes_cli.config.save_env_value', lambda key, value: None)
    assert run('revoke', 'alice', 'rayban') == 1
    assert 'still paired' in capsys.readouterr().out
