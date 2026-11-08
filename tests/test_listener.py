"""The listener binds with public aiohttp calls (not Hermes' private shared_ingress helper) and behaves the same."""
import socket

import pytest


@pytest.mark.asyncio
async def test_a_busy_port_fails_the_connect_and_leaves_no_runner(make_adapter):
    with socket.socket() as busy:
        busy.bind(('127.0.0.1', 0))
        busy.listen()
        adapter = make_adapter(port=busy.getsockname()[1])
        with pytest.raises(OSError):
            await adapter.connect()
        assert adapter._runner is None


def test_no_private_hermes_listener_import():
    from pathlib import Path
    source = (Path(__file__).parent.parent / 'adapter.py').read_text()
    imports = [line for line in source.splitlines() if line.lstrip().startswith(('import ', 'from '))]
    assert not [line for line in imports if 'shared_ingress' in line]
