"""Load the plugin as Hermes does, without shadowing Hermes's tools package."""
import importlib.util
import os
from pathlib import Path
import sys
import types

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ['HERMES_SOURCE'])
sys.path[:] = [p for p in sys.path if Path(p or '.').resolve() != ROOT]
sys.path.insert(0, str(SOURCE))
PACKAGE = 'glasses_plugin'

if (ROOT / '__init__.py').exists():
    spec = importlib.util.spec_from_file_location(
        PACKAGE, ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = plugin
    spec.loader.exec_module(plugin)
else:
    plugin = types.ModuleType(PACKAGE)
    plugin.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = plugin


def register_platform():
    from gateway.platform_registry import PlatformEntry, platform_registry
    if hasattr(plugin, 'register'):
        class Context:
            def register_platform(self, **kw):
                platform_registry.register(PlatformEntry(source='plugin', **kw))
            def register_tool(self, **kw):
                pass
        plugin.register(Context())


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith('GLASSES_') and name != 'GLASSES_TEST_SCRATCH':
            monkeypatch.delenv(name)
    for key, value in {
        'HERMES_HOME': str(tmp_path / 'hermes'),
        'HERMES_GATEWAY_LOCK_DIR': str(tmp_path / 'locks'),
        'XDG_STATE_HOME': str(tmp_path / 'state'),
        'HERMES_GATEWAY_MAX_STARTS': '0', 'HERMES_STATE_DB_GUARD_BYPASS': '1',
        'HERMES_DISABLE_LAZY_INSTALLS': '1',
    }.items():
        monkeypatch.setenv(key, value)
    register_platform()


@pytest.fixture
def make_adapter():
    from glasses_plugin.adapter import GlassesAdapter
    from gateway.config import PlatformConfig
    def make(**extra):
        return GlassesAdapter(PlatformConfig(enabled=True, extra={
            'devices': 'alice:ray=tok-alice-1', 'host': '127.0.0.1', 'port': 0,
            'photo_timeout': 0.1, **extra}))
    return make


@pytest_asyncio.fixture
async def server(make_adapter):
    from aiohttp.test_utils import TestClient, TestServer
    adapter = make_adapter()
    await adapter.connect()
    # Exercise the real handler without another bound listener.
    client = TestClient(TestServer(adapter._app))
    await client.start_server()
    try:
        yield adapter, client
    finally:
        await adapter.disconnect()
        await client.close()


class RootDirectory:
    @pytest.hookimpl(tryfirst=True)
    def pytest_collect_directory(self, path, parent):
        if Path(path).resolve() == ROOT:
            return pytest.Dir.from_parent(parent, path=path)


def pytest_configure(config):
    config.pluginmanager.register(RootDirectory(), 'glasses-root-directory')
