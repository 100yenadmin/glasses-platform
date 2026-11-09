"""Reuse the plugin's isolated-home and real adapter fixtures without editing them."""
import importlib.util
import pytest
from pathlib import Path

_spec = importlib.util.spec_from_file_location('glasses_python_client_fixtures',
                                              Path(__file__).resolve().parents[2] / 'tests' / 'conftest.py')
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)
isolated_home = _fixtures.isolated_home
make_adapter = _fixtures.make_adapter
server = _fixtures.server


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    if not config.pluginmanager.hasplugin('glasses-root-directory'):
        config.pluginmanager.register(_fixtures.RootDirectory(), 'glasses-root-directory')
