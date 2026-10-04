"""Bounded AC-3/E-AC-3 access-unit inventory, retaining boundary fragments.

DVD cell cuts can start in an audio frame. Compare decodable complete frames
separately from those fragments; never silently count a fragment as a frame.
"""
import hashlib
from .source import FormatError

# AC-3 words per frame at 48/44.1/32 kHz, paired 44.1 kHz sizes.
_WORDS = [(64,69,96),(80,87,120),(96,104,144),(112,121,168),
          (128,139,192),(160,174,240),(192,208,288),(224,243,336),
          (256,278,384),(320,348,480),(384,417,576),(448,487,672),
          (512,557,768),(640,696,960),(768,835,1152),(896,975,1344),
          (1024,1114,1536),(1152,1253,1728),(1280,1393,1920)]


def frame_size(header):
    if len(header) < 6 or header[:2] != b'\x0b\x77':
        raise FormatError('Invalid AC-3 sync header')
    bsid = header[5] >> 3
    if 11 <= bsid <= 16:
        return 2*(1+((header[2]&7)<<8 | header[3]))
    if bsid > 16:
        raise FormatError('Invalid AC-3 bitstream version')
    rate, code = header[4] >> 6, header[4] & 63
    if rate == 3 or code >= 38:
        raise FormatError('Invalid AC-3 frame size')
    return 2*(_WORDS[code//2][rate]+(code&1 if rate==1 else 0))


def inventory(chunks):
    pending = bytearray()
    payload = hashlib.sha256()
    frames = hashlib.sha256()
    prefix = bytearray()
    count = frame_bytes = total = 0
    synchronized = False
    for chunk in chunks:
        total += len(chunk);payload.update(chunk);pending.extend(chunk)
        while len(pending) >= 6:
            if not synchronized:
                if pending[:2] != b'\x0b\x77':
                    prefix.append(pending.pop(0))
                    if len(prefix) > 3840:raise FormatError('Audio cell has excessive unsynchronized prefix')
                    continue
                try:size=frame_size(pending[:6])
                except FormatError:
                    prefix.append(pending.pop(0));continue
                synchronized=True
            else:size=frame_size(pending[:6])
            if size < 6:raise FormatError('AC-3 frame is smaller than its header')
            if len(pending) < size:break
            frames.update(pending[:size]);frame_bytes+=size;count+=1;del pending[:size]
    if not count:raise FormatError('No complete AC-3 access unit')
    return dict(payload_sha256=payload.hexdigest(),payload_bytes=total,
                frames_sha256=frames.hexdigest(),frames=count,frame_bytes=frame_bytes,
                prefix_bytes=len(prefix),prefix_sha256=hashlib.sha256(prefix).hexdigest(),
                suffix_bytes=len(pending),suffix_sha256=hashlib.sha256(pending).hexdigest())
