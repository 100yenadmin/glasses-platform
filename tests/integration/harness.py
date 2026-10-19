"""Minimal isolated gateway driver adapted from Hermes's chaos harness."""
import asyncio
from datetime import datetime, timedelta, timezone
import importlib.util
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import tempfile

import aiohttp

ROOT = Path(__file__).resolve().parents[2]


def assert_isolated(env, scratch):
    scratch = Path(scratch).resolve()
    for key in ('HOME', 'HERMES_HOME', 'HERMES_GATEWAY_LOCK_DIR', 'XDG_STATE_HOME'):
        value = env.get(key)
        assert value and Path(value).is_absolute(), f'{key} missing or relative'
        assert Path(value).resolve().is_relative_to(scratch), f'{key} outside scratch'
    for key, expected in {'HERMES_GATEWAY_MAX_STARTS': '0', 'HERMES_STATE_DB_GUARD_BYPASS': '1',
                          'HERMES_DISABLE_LAZY_INSTALLS': '1'}.items():
        assert env.get(key) == expected, f'{key} isolation guard missing'


def hermetic_env(root, source, scratch):
    env = {k: v for k, v in os.environ.items() if not (
        k.endswith(('_API_KEY', '_TOKEN', '_SECRET')) or
        k.startswith(('HERMES_', 'OPENAI', 'ANTHROPIC', 'OPENROUTER', 'NOUS_', 'GLASSES_')) or
        k in ('PYTEST_CURRENT_TEST', 'NOTIFY_SOCKET', 'INVOCATION_ID', 'WATCHDOG_USEC', 'WATCHDOG_PID'))}
    env.update(HOME=str(root / 'home'), HERMES_HOME=str(root / 'hermes'),
               HERMES_GATEWAY_LOCK_DIR=str(root / 'locks'), XDG_STATE_HOME=str(root / 'state'),
               XDG_CONFIG_HOME=str(root / 'config'), XDG_CACHE_HOME=str(root / 'cache'),
               XDG_DATA_HOME=str(root / 'data'), HERMES_GATEWAY_MAX_STARTS='0',
               HERMES_STATE_DB_GUARD_BYPASS='1', HERMES_DISABLE_LAZY_INSTALLS='1',
               HERMES_SOURCE=str(source), PYTHONPATH=str(source), PYTHONUNBUFFERED='1',
               PYTHONDONTWRITEBYTECODE='1', LITELLM_LOCAL_MODEL_COST_MAP='True',
               TMPDIR=str(root), OPENAI_API_KEY='sk-fake-e2e', NO_COLOR='1')
    assert_isolated(env, scratch)
    return env


def load_fake(source):
    spec = importlib.util.spec_from_file_location('glasses_fake_llm', source / 'tests/fakes/fake_llm_provider.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class Gateway:
    def __init__(self, root, source, scratch, fake):
        root = Path(tempfile.mkdtemp(prefix='g-', dir=scratch))
        self.root, self.source, self.scratch = root, source, scratch
        self.port = free_port()
        self.env = hermetic_env(root, source, scratch)
        Path(self.env['HOME']).mkdir(parents=True)
        self.home = Path(self.env['HERMES_HOME'])
        fake.write_hermes_home(self.home, fake.server_url, extra_config=f'''
_config_version: 12
compression:
  enabled: false
memory:
  memory_enabled: false
  user_profile_enabled: false
updates:
  check: false
providers:
  custom:
    request_timeout_seconds: 15
    stale_timeout_seconds: 10
streaming:
  enabled: true
plugins:
  enabled: [glasses-platform]
platforms:
  glasses:
    enabled: true
    extra:
      devices: alice:sim=tok-alice-1
      host: 127.0.0.1
      port: {self.port}
      photo_timeout: 3
platform_toolsets:
  glasses: [terminal, glasses]
terminal:
  backend: local
cron:
  preflight: false
''')
        # A known vision model exercises the pin's native image ingress.
        config = self.home / 'config.yaml'
        config.write_text(config.read_text().replace('default: fake-model', 'default: gpt-4o'))
        target = self.home / 'plugins' / 'glasses-platform'
        shutil.copytree(ROOT, target, ignore=shutil.ignore_patterns('.git', '__pycache__', '.pytest*'))
        self.proc, self.log = None, None

    @property
    def url(self):
        return f'ws://127.0.0.1:{self.port}/voice'

    async def start(self):
        assert_isolated(self.env, self.scratch)
        self.log = (self.root / 'gateway.log').open('wb')
        self.proc = subprocess.Popen([str(self.source / '.venv/bin/python'), '-m', 'gateway.run'],
                                     cwd=self.env['HOME'], env=self.env, stdin=subprocess.DEVNULL,
                                     stdout=self.log, stderr=subprocess.STDOUT)
        async with aiohttp.ClientSession() as session:
            async with asyncio.timeout(90):
                while self.proc.poll() is None:
                    try:
                        async with session.get(f'http://127.0.0.1:{self.port}/health') as response:
                            if response.status == 200:
                                return
                    except aiohttp.ClientError:
                        pass
                    await asyncio.sleep(0.1)
        raise AssertionError('Gateway exited during boot: ' + self.tail())

    async def cron(self):
        assert_isolated(self.env, self.scratch)
        schedule = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
        command = [str(self.source / '.venv/bin/hermes'), 'cron', 'create', schedule,
                   'Send the words cron reminder.', '--deliver', 'glasses:alice', '--repeat', '1']
        proc = await asyncio.create_subprocess_exec(*command, cwd=self.env['HOME'], env=self.env,
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), 30)
        except BaseException:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        assert proc.returncode == 0, output.decode()
        print('cron create: exit=0')

    async def stop(self):
        if self.proc is None:
            return
        started = time.monotonic()
        try:
            if self.proc.poll() is None:
                self.proc.send_signal(signal.SIGTERM)
            code = await asyncio.to_thread(self.proc.wait, timeout=30)
            elapsed = time.monotonic() - started
            # This pin requests supervisor revival after SIGTERM (run.py).
            assert code == 1, f'Gateway exit={code}: {self.tail()}'
            log = (self.home / 'logs/gateway.log').read_text(errors='replace')
            assert 'Gateway stopped (total teardown' in log, log[-3000:]
            assert elapsed < 30
            print('SIGTERM drain: expected exit=1; completed shutdown within 30s')
        except subprocess.TimeoutExpired:
            self.proc.kill()  # only the exact child PID created above
            await asyncio.to_thread(self.proc.wait, timeout=10)
            raise AssertionError('Gateway exceeded SIGTERM drain deadline')
        finally:
            self.log.close()

    def tail(self):
        return (self.root / 'gateway.log').read_text(errors='replace')[-8000:]
