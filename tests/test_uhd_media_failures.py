import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from dvd2uhd.author import decoded_frame_hashes,input_options
from dvd2uhd.audio_frames import inventory
from dvd2uhd.audio_preservation import crc2,prepare,restore
from dvd2uhd.boundary_audio import mux_input
from dvd2uhd.source import FormatError


class MediaFailureTests(unittest.TestCase):
    def test_frame_records_are_separate_from_decoder_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            log=Path(temporary)/'probe.log'
            row=b'0, 0, 0, 1, 12, '+b'a'*32+b'\n'
            result=subprocess.CompletedProcess([],0,b'#format: frame checksums\n'+row,
                                               b'[hevc] diagnostic without commas\n')
            with patch('dvd2uhd.author.subprocess.run',return_value=result):
                self.assertEqual(decoded_frame_hashes('movie.mkv','ffmpeg',log),['a'*32])
            self.assertIn('without commas',log.read_text())
            for stdout in (b'garbage',b'# no decoded frames'):
                result=subprocess.CompletedProcess([],0,stdout,b'')
                with patch('dvd2uhd.author.subprocess.run',return_value=result),self.assertRaises(FormatError):
                    decoded_frame_hashes('movie.mkv','ffmpeg',log)
            self.assertEqual(input_options('cell.VOB'),['-f','mpeg'])
            self.assertEqual(input_options('movie.m2ts'),[])

    def test_false_sync_in_cut_frame_prefix_does_not_hide_real_complete_frames(self):
        frame=bytes.fromhex('0b7700000040')+bytes(range(122))
        prefix=b'prefix'+bytes.fromhex('0b7700000040')+b'not-a-frame'*14
        raw=prefix+frame*5+frame[:39]
        result=inventory(raw[i:i+7] for i in range(0,len(raw),7))
        self.assertEqual(result['prefix_bytes'],len(prefix))
        self.assertEqual(result['frames'],5)
        self.assertEqual(result['suffix_bytes'],39)
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'audio.ac3';path.write_bytes(raw)
            normalized=mux_input(path)
            self.assertEqual(normalized.read_bytes(),frame*5)
            self.assertEqual(path.read_bytes(),raw)
        with self.assertRaises(FormatError):inventory([frame+b'junkjunk'+frame])

    def test_crc_preparation_preserves_source_and_records_only_changed_words(self):
        frame=bytes.fromhex('0b7700000040')+bytes(range(122))
        frame=frame[:-2]+crc2(frame)
        damaged=frame[:-1]+bytes([frame[-1]^1])
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'source.ac3';path.write_bytes(frame+damaged+frame)
            output,changes=prepare(path)
            self.assertEqual(output.read_bytes(),frame*3)
            self.assertEqual(path.read_bytes(),frame+damaged+frame)
            self.assertEqual(len(changes),1)
            self.assertEqual(changes[0]['offset'],254)
            self.assertEqual(changes[0]['frame_sha256'],hashlib.sha256(damaged).hexdigest())

    def transport(self):
        data=bytearray(b'x'*300);data[174:176]=b'AB'
        pes=b'\x00\x00\x01\xbd'+(len(data)+3).to_bytes(2,'big')+b'\x80\x00\x00'
        first=b'\0'*4+bytes([0x47,0x51,0,0x10])+pes+data[:175]
        second=b'\0'*4+bytes([0x47,0x11,0,0x31,58])+b'\0'*58+data[175:]
        self.assertEqual(len(first),192);self.assertEqual(len(second),192)
        return first+second

    def test_crc_restore_across_transport_packets_checks_bytes_and_keeps_headers(self):
        raw=self.transport();repairs={0x1100:[dict(offset=174,temporary='4142',original='3132')]}
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'movie.m2ts';path.write_bytes(raw)
            self.assertEqual(restore(path,repairs),1)
            expected=bytearray(raw);expected[191]=ord('1');expected[259]=ord('2')
            self.assertEqual(path.read_bytes(),expected)
            for damaged in (raw[:192],raw[:191]+b'Z'+raw[192:],raw[:199]+b'\x33'+raw[200:]):
                path.write_bytes(damaged)
                with self.assertRaises(ValueError):restore(path,repairs)
                self.assertEqual(path.read_bytes(),damaged)
