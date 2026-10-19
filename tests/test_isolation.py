from pathlib import Path
import pytest
from glasses_plugin.tests.integration.harness import assert_isolated, hermetic_env


def test_isolation_guards(tmp_path, monkeypatch):
    monkeypatch.setenv('REAL_API_KEY', 'fake-parent')
    monkeypatch.setenv('REAL_TOKEN', 'fake-parent')
    monkeypatch.setenv('HERMES_AMBIENT', 'unsafe')
    env = hermetic_env(tmp_path / 'run', Path('source'), tmp_path)
    assert 'REAL_API_KEY' not in env and 'REAL_TOKEN' not in env and 'HERMES_AMBIENT' not in env
    assert_isolated(env, tmp_path)
    for key in ('HOME', 'HERMES_HOME', 'HERMES_GATEWAY_LOCK_DIR', 'XDG_STATE_HOME',
                'HERMES_GATEWAY_MAX_STARTS', 'HERMES_STATE_DB_GUARD_BYPASS', 'HERMES_DISABLE_LAZY_INSTALLS'):
        bad = dict(env)
        bad.pop(key)
        with pytest.raises(AssertionError):
            assert_isolated(bad, tmp_path)
    with pytest.raises(AssertionError):
        assert_isolated({**env, 'HERMES_GATEWAY_LOCK_DIR': str(tmp_path.parent)}, tmp_path)
