"""Stock xiaozhi voice-box simulator; all printed lines are received device JSON frames."""
import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
from uuid import uuid4

import aiohttp

# Also runnable directly, without importing the Hermes-dependent plugin entrypoint.
_spec = importlib.util.spec_from_file_location('xiaozhi_ogg', Path(__file__).resolve().parents[1] / 'ogg.py')
_ogg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ogg)
mux_opus, demux_opus = _ogg.mux_opus, _ogg.demux_opus
DEFAULT_WAV = Path(__file__).with_name('sample-voice.wav')


async def wav_packets(path):
    process = await asyncio.create_subprocess_exec(
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', str(path), '-vn',
        '-ac', '1', '-ar', '16000', '-c:a', 'libopus', '-frame_duration', '60', '-f', 'ogg', 'pipe:1',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 20)
        if process.returncode:
            raise ValueError('ffmpeg could not read the WAV')
        return demux_opus(output)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def run(url='ws://127.0.0.1:8765/xiaozhi', token='', ota=None, wav=DEFAULT_WAV,
              out='reply.ogg', text_only=False, timeout=60, emit=print):
    async with aiohttp.ClientSession() as session:
        if ota:
            async with session.post(ota, json={'application': {'version': 'sim-1'}}) as response:
                response.raise_for_status()
                config = (await response.json())['websocket']
                url, token = config['url'], config['token']
        headers = {'Authorization': 'Bearer ' + token, 'Protocol-Version': '1',
                   'Device-Id': 'simulator', 'Client-Id': uuid4().hex}
        packets = [] if text_only else await wav_packets(wav)
        async with session.ws_connect(url, headers=headers) as ws:
            await ws.send_json({'type': 'hello', 'version': 1, 'transport': 'websocket',
                                'audio_params': {'format': 'opus', 'sample_rate': 16000,
                                                 'channels': 1, 'frame_duration': 60}})
            hello = await ws.receive_json(timeout=timeout)
            emit(json.dumps(hello))
            if hello.get('type') != 'hello':
                return 2

            async def upload():
                if not text_only:
                    await ws.send_json({'type': 'listen', 'state': 'start', 'mode': 'manual'})
                    for packet in packets:
                        await ws.send_bytes(packet)
                        await asyncio.sleep(0.06)
                    await ws.send_json({'type': 'listen', 'state': 'stop'})

            sender = asyncio.create_task(upload())
            reply = []
            try:
                async with asyncio.timeout(timeout):
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            frame = json.loads(msg.data)
                            emit(json.dumps(frame))
                            if frame.get('type') == 'tts' and frame.get('state') == 'stop':
                                return 0
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            reply.append(msg.data)
                return 2
            except TimeoutError:
                return 2
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
                if not text_only:
                    Path(out).write_bytes(mux_opus(reply, sample_rate=24000))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='ws://127.0.0.1:8765/xiaozhi')
    parser.add_argument('--token', default=os.environ.get('GLASSES_SIM_TOKEN', ''))
    parser.add_argument('--ota')
    parser.add_argument('--wav', default=str(DEFAULT_WAV))
    parser.add_argument('--out', default='reply.ogg')
    parser.add_argument('--text-only', action='store_true')
    parser.add_argument('--timeout', type=float, default=60)
    args = parser.parse_args()
    try:
        code = asyncio.run(run(**vars(args)))
    except (aiohttp.ClientError, OSError, ValueError):
        print('Simulator could not complete the exchange; check the address, token, WAV and ffmpeg.')
        code = 2
    raise SystemExit(code)


if __name__ == '__main__':
    main()
