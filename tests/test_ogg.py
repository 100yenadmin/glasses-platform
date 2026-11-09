import struct
import shutil
import subprocess

import pytest
from glasses_plugin.ogg import crc32, mux_opus, demux_opus


def pages(data):
    offset = 0
    while offset < len(data):
        count = data[offset + 26]
        end = offset + 27 + count + sum(data[offset + 27:offset + 27 + count])
        yield data[offset:end]
        offset = end
    assert offset == len(data)


def test_mux_round_trip_crc_lacing_granules_and_headers():
    packets = [b'a' * 100, b'b' * 255, b'c' * 65025, b'd' * 70000, b'']
    data = mux_opus(packets)
    assert demux_opus(data) == packets
    assert crc32(b'123456789') == 0x89a1897f
    completed = []
    for seq, page in enumerate(pages(data)):
        assert struct.unpack_from('<I', page, 18)[0] == seq
        copy = bytearray(page)
        expected = struct.unpack_from('<I', copy, 22)[0]
        struct.pack_into('<I', copy, 22, 0)
        assert crc32(copy) == expected
        granule = struct.unpack_from('<Q', page, 6)[0]
        if granule != 0xffffffffffffffff:
            completed.append(granule)
    assert completed == [0, 0, 2880, 5760, 8640, 11520, 14400]
    rows = list(pages(data))
    assert rows[0][5] == 2 and rows[-1][5] == 4
    assert any(page[5] & 1 for page in rows)
    assert b'OpusHead\x01\x01\x00\x00\x80\x3e\x00\x00' in rows[0]
    assert b'OpusTags' in rows[1]


def test_mux_granules_use_48k_clock_independent_of_input_rate():
    data = mux_opus([b'x', b'y'], sample_rate=24000, channels=2, frame_ms=20)
    assert [struct.unpack_from('<Q', p, 6)[0] for p in pages(data)] == [0, 0, 960, 1920]
    assert demux_opus(mux_opus([])) == []


@pytest.mark.parametrize('data', [b'', b'garbage', mux_opus([b'x'])[:-1], mux_opus([b'x'])[:-1] + b'y'])
def test_demux_rejects_truncation_or_corruption(data):
    with pytest.raises(ValueError):
        demux_opus(data)


def test_demux_ffmpeg_file(tmp_path):
    if not shutil.which('ffmpeg'):
        pytest.skip('ffmpeg not installed')
    from pathlib import Path
    wav = Path(__file__).resolve().parents[1] / 'sim' / 'sample-voice.wav'
    output = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(wav), '-c:a', 'libopus',
                             '-frame_duration', '60', '-f', 'ogg', 'pipe:1'], capture_output=True, check=True).stdout
    packets = demux_opus(output)
    assert len(packets) > 20 and all(not p.startswith(b'Opus') for p in packets)
    assert demux_opus(mux_opus(packets)) == packets
