import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from glasses_plugin.sim.glasses_sim import run, log_frame
from glasses_plugin.tests.integration.harness import Gateway, load_fake

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get('GLASSES_PLATFORM_INTEGRATION') != '1' or not os.environ.get('HERMES_SOURCE'),
    reason='opt-in real gateway test')]


@pytest.mark.asyncio
async def test_gateway_text_photo_cron_and_drain(tmp_path):
    source = Path(os.environ['HERMES_SOURCE'])
    scratch = Path(os.environ['GLASSES_TEST_SCRATCH'])
    fake = load_fake(source)
    with fake.FakeLLMServer([fake.Text('hello-v1'), fake.Text('hello-v0'),
                             fake.Text('A colored scene.'), fake.Text('cron reminder')]) as model:
        gateway = Gateway(tmp_path, source, scratch,
                          SimpleNamespace(write_hermes_home=fake.write_hermes_home, server_url=model.base_url))
        frames = []
        def emit(direction, frame):
            frames.append((direction, frame))
            log_frame(direction, frame)
        try:
            await gateway.start()
            for legacy, expected in ((False, 'hello-v1'), (True, 'hello-v0')):
                frames.clear()
                assert await run(gateway.url, 'tok-alice-1', legacy=legacy, say=['hello'], timeout=60, emit=emit) == 0
                assert next(f['text'] for d, f in frames if d == 'in' and f['type'] == 'response') == expected
                print('text protocol ' + ('v0' if legacy else 'v1') + ': reply matched')
            frames.clear()
            assert await run(gateway.url, 'tok-alice-1', say=['what am I looking at?'], timeout=60, emit=emit) == 0
            assert any(d == 'in' and f['type'] == 'capture_photo' for d, f in frames)
            assert any(d == 'in' and f.get('text') == 'A colored scene.' for d, f in frames)
            requests = model.main_requests()
            image_parts = [part for req in requests for msg in req.get('messages', [])
                           if isinstance(msg.get('content'), list) for part in msg['content']
                           if part.get('type') == 'image_url']
            if image_parts:
                assert image_parts[-1]['image_url']['url'].startswith('data:image/jpeg;base64,')
                print('photo: native image_url reached fake model; reply matched')
            else:
                auxiliary_images = [part for req in model.aux_requests() for msg in req.get('messages', [])
                                    if isinstance(msg.get('content'), list) for part in msg['content']
                                    if part.get('type') == 'image_url']
                assert auxiliary_images, 'JPEG must reach the vision pre-analysis model'
                assert auxiliary_images[-1]['image_url']['url'].startswith('data:image/jpeg;base64,')
                main_text = json.dumps(requests[-1]['messages'])
                assert 'The user sent an image' in main_text
                assert 'Fake summary of the earlier conversation.' in main_text
                print('photo: JPEG reached vision pre-analysis; its text reached main model; reply matched')
            frames.clear()
            assert await run(gateway.url, 'tok-alice-1', wait_push=150, timeout=150,
                             emit=emit, on_ready=gateway.cron) == 0
            pushes = [f for d, f in frames if d == 'in' and f['type'] == 'push']
            assert any('cron reminder' in f['text'] and f['origin'] == 'cron' for f in pushes)
            print('cron: push received through live adapter')
        except BaseException:
            print(gateway.tail() if gateway.proc else 'gateway not spawned')
            raise
        finally:
            await gateway.stop()
