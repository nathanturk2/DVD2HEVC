"""Native still retention changes metadata, never movie data or DVD timing."""
import struct
import tempfile
import unittest
from pathlib import Path
from dvd2uhd.bdmv import native_still_playlist,native_still_playlists,hold_native_stills,playlist_times,primary_streams


def playlist(count=1):
    item=b'00036M2TS\0\1\0'+struct.pack('>II',27000000,27000900)+b'\0'*12+b'\0\x0e'+b'\0'*14
    body=b'\0\0'+struct.pack('>HH',count,0)+(struct.pack('>H',len(item))+item)*count
    header=bytearray(64);header[:8]=b'MPLS0300';header[8:12]=(64).to_bytes(4,'big')
    return bytes(header)+struct.pack('>I',len(body))+body


class NativeStillTests(unittest.TestCase):
    def test_only_reserved_still_fields_change_and_repeat_is_idempotent(self):
        original=playlist();fixed=native_still_playlist(original)
        self.assertEqual(len(fixed),len(original))
        self.assertEqual([n for n,(a,b) in enumerate(zip(original,fixed)) if a!=b],[64+10+31])
        self.assertEqual(playlist_times(fixed),playlist_times(original))
        self.assertEqual(primary_streams(fixed),primary_streams(original))
        self.assertEqual(native_still_playlist(fixed),fixed)
        for invalid in (original[:40],playlist(2),b'not a playlist'):
            with self.assertRaises(ValueError):native_still_playlist(invalid)

    def test_wait_selection_includes_cell_and_final_pgc_stills_only(self):
        def cell(i,wait=0,repeated=False):
            return dict(playlist=i,still=wait,still_repeated=repeated,duration=43200,presentation_ms=480)
        graph=dict(pgcs=[dict(still=3,cells=[cell(1),cell(2)]),
                         dict(still=0,cells=[cell(3,255),cell(4,5,True),cell(5,0)])])
        self.assertEqual(native_still_playlists(graph),[2,3])
        graph['pgcs'][0]['cells'][-1]['end_ms']=480
        with self.assertRaisesRegex(ValueError,'independent'):native_still_playlists(graph)

    def test_both_playlist_copies_are_held_without_touching_payload(self):
        graph=dict(pgcs=[dict(still=0,cells=[dict(playlist=36,still=255,duration=43200)])])
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);media=root/'BDMV/STREAM/00036.m2ts';media.parent.mkdir(parents=True);media.write_bytes(b'original video')
            for prefix in ('','BACKUP/'):
                path=root/('BDMV/'+prefix+'PLAYLIST/00036.mpls');path.parent.mkdir(parents=True);path.write_bytes(playlist())
            self.assertEqual(hold_native_stills(root,graph),[36])
            self.assertEqual((root/'BDMV/PLAYLIST/00036.mpls').read_bytes(),(root/'BDMV/BACKUP/PLAYLIST/00036.mpls').read_bytes())
            self.assertEqual(media.read_bytes(),b'original video')
