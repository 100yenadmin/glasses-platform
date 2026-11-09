"""Reference smart-glasses companion client (protocol v1 or legacy v0)."""
import argparse
import asyncio
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import textwrap
from uuid import uuid4

import aiohttp
from yarl import URL

DEFAULT_PHOTO = Path(__file__).resolve().parent / 'sample-photo.jpg'  # an airport gate sign


def elide(frame):
    if isinstance(frame, dict):
        return {k: (f'<{len(v)} bytes>' if k == 'data' and isinstance(v, str) else elide(v))
                for k, v in frame.items()}
    if isinstance(frame, list):
        return [elide(v) for v in frame]
    return frame


def log_frame(direction, frame):
    print(json.dumps({'t': datetime.now(timezone.utc).isoformat(), 'dir': direction,
                      'frame': elide(frame)}), flush=True)


def pretty_frame(direction, frame):
    """A readable transcript of the same frames, for people watching (status and stream frames are left out)."""
    kind, text = frame.get('type'), frame.get('text') or frame.get('message') or ''
    if direction == 'out':
        line = {'query': ('you', text),
                'photo': ('glasses', f'photo sent ({len(frame.get("data", "")) * 3 // 4 // 1024} KB)'),
                'photo_error': ('glasses', 'no photo: ' + text),
                'new_session': ('you', 'new session')}.get(kind)
    else:
        line = {'capture_photo': ('hermes', 'asks the glasses for a photo'),
                'response': ('hermes', text),
                'push': ('hermes', f'[{frame.get("origin", "push")}] {text}'),
                'session_reset': ('hermes', 'new session started'),
                'error': ('hermes', 'error: ' + text)}.get(kind)
    if line:
        who, said = line
        prefix = f'{datetime.now().strftime("%H:%M:%S")}  {who:<8} '
        print(textwrap.fill(' '.join(said.split()), width=100, initial_indent=prefix,
                            subsequent_indent=' ' * len(prefix)), flush=True)


async def run(url='ws://127.0.0.1:8765/voice', token='', legacy=False, device='sim',
              no_camera=False, say=(), photo=DEFAULT_PHOTO, inline_photo=None,
              new_session=False, wait_push=0, timeout=120, emit=log_frame, on_ready=None):
    """Return a CLI exit code; ``emit`` and ``on_ready`` also support in-process tests."""
    token = token or URL(url).query.get('token', '')  # a connect URL from `hermes glasses pair` carries it
    url = str(URL(url).without_query_params('token'))
    headers = {} if legacy else {'Authorization': 'Bearer ' + token}
    if legacy:
        url = str(URL(url).update_query(token=token))
    saw_push = False
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(url, headers=headers, max_msg_size=16 * 1024 * 1024) as ws:
            async def send(frame):
                emit('out', frame)
                await ws.send_json(frame)

            async def receive():
                nonlocal saw_push
                msg = await ws.receive(timeout=timeout)
                if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    raise ConnectionError(ws.close_code)
                if msg.type != aiohttp.WSMsgType.TEXT:
                    return {}
                frame = json.loads(msg.data)
                emit('in', frame)
                if frame.get('type') == 'capture_photo':
                    try:
                        if no_camera:
                            raise OSError('no camera')
                        answer = {'type': 'photo', 'data': base64.b64encode(Path(photo).read_bytes()).decode()}
                    except OSError:
                        answer = {'type': 'photo_error', 'message': 'Camera unavailable.'}
                    if 'request' in frame:
                        answer['request'] = frame['request']
                    await send(answer)
                if frame.get('type') == 'push' or (legacy and frame.get('type') == 'response' and not waiting_turn):
                    saw_push = True
                return frame

            waiting_turn = False
            try:
                await receive()  # welcome, or 4401
                if not legacy:
                    await send({'type': 'hello', 'protocol': 1,
                                'device': {'name': device, 'capabilities': {'camera': not no_camera}}})
                    while (await receive()).get('type') != 'hello_ack':
                        pass
                if on_ready is not None:
                    await on_ready()
                if new_session:
                    await send({'type': 'new_session'})
                    while (await receive()).get('type') not in ('session_reset', 'error'):
                        pass
                for text in say:
                    turn = uuid4().hex
                    frame = {'type': 'query', 'text': text, 'id': turn}
                    if inline_photo and not legacy:
                        frame['image'] = {'mime': 'image/jpeg', 'data': base64.b64encode(Path(inline_photo).read_bytes()).decode()}
                    waiting_turn = True
                    await send(frame)
                    async with asyncio.timeout(timeout):
                        while True:
                            reply = await receive()
                            if reply.get('type') == 'response' and (legacy or (reply.get('turn') == turn and reply.get('final'))):
                                break
                    waiting_turn = False
                if wait_push:
                    async with asyncio.timeout(wait_push):
                        while not saw_push:
                            await receive()
                return 0
            except TimeoutError:
                return 2
            except ConnectionError as error:
                return 3 if error.args[0] == 4401 else 2


async def chat(url, token, device='terminal', photo=DEFAULT_PHOTO, emit=pretty_frame, ask=input):
    """Talk to the agent line by line as a pair of glasses with a camera. Pushes (reminders, cron results) print
    whenever they arrive. An empty line or end of input quits."""
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(url, headers={'Authorization': 'Bearer ' + token},
                                      max_msg_size=16 * 1024 * 1024) as ws:
            answered, ready, turn = asyncio.Event(), asyncio.Event(), None

            async def send(frame):
                if frame['type'] != 'query':  # the typed line is already on screen
                    emit('out', frame)
                await ws.send_json(frame)

            async def reader():
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    frame = json.loads(msg.data)
                    kind = frame.get('type')
                    if kind == 'welcome':
                        await send({'type': 'hello', 'protocol': 1,
                                    'device': {'name': device, 'capabilities': {'camera': True}}})
                        continue
                    emit('in', frame)
                    if kind == 'hello_ack':
                        ready.set()
                    elif kind == 'capture_photo':
                        try:
                            answer = {'type': 'photo', 'data': base64.b64encode(Path(photo).read_bytes()).decode()}
                        except OSError:
                            answer = {'type': 'photo_error', 'message': 'Camera unavailable.'}
                        if 'request' in frame:
                            answer['request'] = frame['request']
                        await send(answer)
                    elif kind == 'response' and frame.get('final') and frame.get('turn') == turn:
                        answered.set()
                    elif kind == 'error' and turn:
                        answered.set()
                ready.set(); answered.set()  # the socket closed: wake whoever waits
                return ws.close_code

            task = asyncio.create_task(reader())
            try:
                await ready.wait()
                if task.done():
                    return 3 if task.result() == 4401 else 2
                while not task.done():
                    text = (await asyncio.to_thread(ask, '')).strip()
                    if not text:
                        return 0
                    turn = uuid4().hex
                    answered.clear()
                    await send({'type': 'query', 'id': turn, 'text': text})
                    await answered.wait()
                return 3 if task.result() == 4401 else 2
            except EOFError:
                return 0
            finally:
                task.cancel()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='ws://127.0.0.1:8765/voice')
    parser.add_argument('--token', default=os.environ.get('GLASSES_SIM_TOKEN', ''))
    parser.add_argument('--legacy', action='store_true')
    parser.add_argument('--device', default='sim')
    parser.add_argument('--no-camera', action='store_true')
    parser.add_argument('--say', action='append', default=[])
    parser.add_argument('--photo', default=str(DEFAULT_PHOTO))
    parser.add_argument('--inline-photo')
    parser.add_argument('--new-session', action='store_true')
    parser.add_argument('--wait-push', type=float, default=0)
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--pretty', action='store_true', help='print a readable transcript instead of JSON lines')
    args = vars(parser.parse_args())
    emit = pretty_frame if args.pop('pretty') else log_frame
    raise SystemExit(asyncio.run(run(**args, emit=emit)))


if __name__ == '__main__':
    main()
