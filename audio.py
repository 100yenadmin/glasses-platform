"""Dependency-free audio codecs; Hermes owns speech providers and their configuration."""
import asyncio
import base64
import binascii
import io
from pathlib import Path
import shutil
import wave

MIME_EXT = {'audio/wav': '.wav', 'audio/ogg': '.ogg', 'audio/mpeg': '.mp3',
            'audio/mp4': '.m4a', 'audio/webm': '.webm', 'audio/flac': '.flac'}


def decode(recording, max_bytes):
    mime = recording.get('mime')
    if mime not in MIME_EXT and mime != 'audio/pcm':
        raise ValueError('Unknown audio MIME')
    encoded = recording.get('data')
    if not isinstance(encoded, str) or len(encoded) > 4 * ((max_bytes + 2) // 3):
        raise ValueError('Audio too large or missing')
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError('Invalid audio') from error
    if not data or len(data) > max_bytes:
        raise ValueError('Audio too large or empty')
    if mime == 'audio/pcm':
        rate = recording.get('rate', 16000)
        if type(rate) is not int or not 8000 <= rate <= 48000 or len(data) % 2:
            raise ValueError('Invalid PCM recording')
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(data)
        return buffer.getvalue(), '.wav'
    return data, MIME_EXT[mime]


def output_mimes(capabilities):
    """The device's playable formats, in its order, that this server can produce."""
    formats = capabilities.get('audio_out')
    if not isinstance(formats, list):
        return []
    ffmpeg = shutil.which('ffmpeg')
    return [mime for mime in formats if mime == 'audio/mpeg' or (mime in ('audio/ogg', 'audio/wav') and ffmpeg)]


def output_mime(capabilities):
    return next(iter(output_mimes(capabilities)), None)


async def encode_files(paths, mime):
    """Join Hermes' MP3 chunks, optionally transcode to the negotiated wire format."""
    ffmpeg = shutil.which('ffmpeg')
    all_mp3 = all(Path(path).suffix.lower() == '.mp3' for path in paths)
    if mime == 'audio/mpeg' and all_mp3 and (len(paths) == 1 or not ffmpeg):  # MP3 frames join byte-wise
        return await asyncio.to_thread(lambda: b''.join(Path(path).read_bytes() for path in paths))
    if not ffmpeg:
        raise ValueError('Audio format unavailable')
    args = [ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error']
    for path in paths:
        args.extend(['-i', str(path)])
    if len(paths) > 1:
        inputs = ''.join(f'[{i}:a]' for i in range(len(paths)))
        args.extend(['-filter_complex', f'{inputs}concat=n={len(paths)}:v=0:a=1[out]', '-map', '[out]'])
    codec, container = {'audio/mpeg': ('libmp3lame', 'mp3'), 'audio/ogg': ('libopus', 'ogg'),
                        'audio/wav': ('pcm_s16le', 'wav')}[mime]
    args.extend(['-c:a', codec, '-f', container, 'pipe:1'])
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                   stderr=asyncio.subprocess.PIPE)
    try:
        data, _ = await asyncio.wait_for(process.communicate(), 30)
        if process.returncode:
            raise ValueError('Audio conversion failed')
        return data
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
