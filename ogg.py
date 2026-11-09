"""Small Ogg Opus container helpers. Granule positions always use the 48 kHz clock."""
import struct


def _crc_entry(byte):
    crc = byte << 24
    for _ in range(8):
        crc = ((crc << 1) ^ (0x04C11DB7 if crc & 0x80000000 else 0)) & 0xffffffff
    return crc


_CRC_TABLE = tuple(_crc_entry(byte) for byte in range(256))


def crc32(data):
    """Ogg CRC-32: polynomial 0x04C11DB7, no reflection, initial value zero."""
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xffffffff) ^ _CRC_TABLE[(crc >> 24) ^ byte]
    return crc


def mux_opus(packets, sample_rate=16000, channels=1, frame_ms=60):
    if channels not in (1, 2) or sample_rate <= 0 or frame_ms <= 0:
        raise ValueError('Invalid Opus parameters')
    packets = list(packets)
    head = b'OpusHead' + struct.pack('<BBHIhB', 1, channels, 0, sample_rate, 0, 0)
    tags = b'OpusTags' + struct.pack('<I', 6) + b'Hermes' + struct.pack('<I', 0)
    output, seq = bytearray(), 0
    for index, packet in enumerate([head, tags, *packets]):
        laces = [255] * (len(packet) // 255) + [len(packet) % 255]
        offset, continued = 0, False
        while laces:
            segments, laces = laces[:255], laces[255:]
            final = not laces
            flags = (1 if continued else 0) | (2 if seq == 0 else 0)
            if final and index == len(packets) + 1:
                flags |= 4
            granule = max(0, index - 1) * round(48 * frame_ms) if final else 0xffffffffffffffff
            body = packet[offset:offset + sum(segments)]
            page = bytearray(struct.pack('<4sBBQIIIB', b'OggS', 0, flags, granule, 1, seq, 0, len(segments)))
            page += bytes(segments) + body
            struct.pack_into('<I', page, 22, crc32(page))
            output += page
            offset += len(body)
            seq += 1
            continued = True
    return bytes(output)


def demux_opus(data):
    packets, pending, offset, serial = [], bytearray(), 0, None
    while offset < len(data):
        if len(data) - offset < 27 or data[offset:offset + 5] != b'OggS\x00':
            raise ValueError('Invalid Ogg page')
        count = data[offset + 26]
        laces = data[offset + 27:offset + 27 + count]
        end = offset + 27 + count + sum(laces)
        if len(laces) != count or end > len(data):
            raise ValueError('Truncated Ogg page')
        page = bytearray(data[offset:end])
        expected = struct.unpack_from('<I', page, 22)[0]
        struct.pack_into('<I', page, 22, 0)
        if crc32(page) != expected:
            raise ValueError('Invalid Ogg CRC')
        page_serial = struct.unpack_from('<I', page, 14)[0]
        if serial is None:
            serial = page_serial
        if page_serial != serial or bool(page[5] & 1) != bool(pending):
            raise ValueError('Unsupported Ogg stream or continuation')
        pos = 27 + count
        for size in laces:
            pending += page[pos:pos + size]
            pos += size
            if size < 255:
                packets.append(bytes(pending))
                pending.clear()
        offset = end
    if pending or len(packets) < 2 or not packets[0].startswith(b'OpusHead') or not packets[1].startswith(b'OpusTags'):
        raise ValueError('Incomplete Ogg Opus stream')
    return packets[2:]
