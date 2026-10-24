"""Tiny HEVC pictures must drain without altering encoded pictures or audio."""
import unittest
from dvd2uhd.transport import terminal_delimiter_edits,EOS,AUD


def packet(payload,pid=0x1011,start=False):
    header=b'\0'*4+bytes([0x47,(pid>>8)|(64 if start else 0),pid&255,0x10 if len(payload)==184 else 0x30])
    if len(payload)==184:return header+payload
    size=183-len(payload)
    return header+bytes([size,0])+b'\xff'*(size-1)+payload


def payloads(data,pid=0x1011):
    result=b''
    for at in range(0,len(data),192):
        p=data[at:at+192]
        if ((p[5]&31)<<8)|p[6]!=pid:continue
        pos=9+p[8] if p[7]&0x20 else 8
        result+=p[pos:]
    return result


def tiny_stream(two_packets=False,stuffing=None):
    # A complete video PES, legacy synthetic EOS and a separate audio packet.
    video=b'\0\0\1\x26\1'+b'picture-data'*4
    if two_packets:video+=b'x'*(210-len(video))
    if stuffing is not None:video+=b'x'*(182-stuffing-9-7-len(video))
    es=video+EOS+b'\x80';pes=b'\0\0\1\xe1'+(len(es)+3).to_bytes(2,'big')+b'\x80\0\0'+es
    if two_packets:parts=[packet(pes[:184],start=True),packet(pes[184:])]
    else:parts=[packet(pes,start=True)]
    return b''.join(parts)+packet(b'original audio',pid=0x1100,start=True),video


class TerminalDelimiterTests(unittest.TestCase):
    def test_reuses_existing_aud_without_padding_or_audio_packet_movement(self):
        original,video=tiny_stream(stuffing=0)
        pes=payloads(original);pes=pes[:4]+(len(pes)-6+len(AUD)).to_bytes(2,'big')+pes[6:9]+AUD+pes[9:]
        original=packet(pes[:184],start=True)+packet(pes[184:])+original[-192:]
        fixed=bytearray(original)
        for offset,p in terminal_delimiter_edits(original):fixed[offset:offset+len(p)]=p
        result=payloads(fixed)
        self.assertEqual(len(fixed),len(original))
        self.assertEqual(result[:9],pes[:9])
        self.assertEqual(result[9:],video+EOS+b'\x80'+AUD)
        self.assertEqual(fixed[-192:],original[-192:])
        self.assertEqual(terminal_delimiter_edits(fixed),[])

    def test_preserves_pictures_audio_and_timestamps_in_one_or_two_packets(self):
        for split in (False,True):
            original,video=tiny_stream(split);fixed=bytearray(original)
            for offset,p in terminal_delimiter_edits(original):fixed[offset:offset+192]=p
            fixed=bytes(fixed);pes=payloads(fixed)
            self.assertEqual(len(fixed),len(original))
            self.assertEqual(pes[9:],video+EOS+AUD)
            self.assertEqual(int.from_bytes(pes[4:6],'big'),len(pes)-6)
            self.assertEqual(payloads(fixed,0x1100),payloads(original,0x1100))
            self.assertEqual(fixed[-192:],original[-192:])
            self.assertEqual(terminal_delimiter_edits(fixed),[])

    def test_tight_packet_uses_valid_three_byte_annex_b_prefixes(self):
        original,video=tiny_stream(stuffing=4);fixed=bytearray(original)
        for offset,p in terminal_delimiter_edits(original):fixed[offset:offset+192]=p
        pes=payloads(fixed)
        self.assertEqual(pes[9:],video+EOS[1:]+AUD[1:])
        self.assertEqual(int.from_bytes(pes[4:6],'big'),len(pes)-6)
        self.assertEqual(terminal_delimiter_edits(fixed),[])

    def test_malformed_transport_lengths_scrambling_and_unsafe_stuffing_rejected(self):
        original,_=tiny_stream();variants=[original[:-1]]
        bad=bytearray(original);bad[4]=0;variants.append(bad)
        bad=bytearray(original);bad[7]|=0x80;variants.append(bad)
        bad=bytearray(original);bad[9]|=0x10;variants.append(bad)
        bad=bytearray(original);pos=9+bad[8];bad[pos+4:pos+6]=b'\0\x01';variants.append(bad)
        for data in variants:
            with self.assertRaises(ValueError):terminal_delimiter_edits(data)
