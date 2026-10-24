"""Native still retention changes metadata, never movie data or DVD timing."""
import struct
import tempfile
import unittest
from pathlib import Path
from dvd2uhd.bdmv import native_still_playlist,native_still_playlists,hold_native_stills,playlist_times,primary_streams,audio_presentation_playlist,audio_presentations,extend_audio_presentations


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
                         dict(still=0,cells=[cell(3,255),cell(4,5,True),cell(5,0),cell(6,0,True)])])
        self.assertEqual(native_still_playlists(graph),[2,3,4,6])
        graph['pgcs'][1]['cells'][-1]['end_ms']=480
        self.assertEqual(native_still_playlists(graph),[2,3,4])
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


class AudioPresentationTests(unittest.TestCase):
    def test_full_audio_timing_changes_only_out_time_and_preserves_native_hold(self):
        original=native_still_playlist(playlist());fixed=audio_presentation_playlist(original,40000)
        self.assertEqual(len(fixed),len(original))
        self.assertTrue(all(64+10+18<=n<64+10+22 for n,(a,b) in enumerate(zip(original,fixed)) if a!=b))
        self.assertEqual(playlist_times(fixed),(27000000,28800000))
        self.assertEqual(primary_streams(fixed),primary_streams(original))
        self.assertEqual(fixed[64+10+31:64+10+34],original[64+10+31:64+10+34])
        self.assertEqual(audio_presentation_playlist(fixed,40000),fixed)
        for millis in (0,-1,True,1.5,86400001,1000):
            with self.assertRaises(ValueError):audio_presentation_playlist(original,millis)
        with self.assertRaises(ValueError):audio_presentation_playlist(playlist(2),40000)
        with self.assertRaises(ValueError):audio_presentation_playlist(fixed,41000)

    def test_selects_only_independent_unambiguous_audio_cells_with_short_playlists(self):
        def cell(i,**kwargs):
            return dict(playlist=i,duration=3600000,presentation_ms=40000,audio_streams=[0],**kwargs)
        cells=[cell(1),cell(2,end_ms=40000),cell(3),cell(4),cell(4)]
        cells[2]['audio_streams']=[];cells[-1]['presentation_ms']=29400
        cells.extend([cell(5),cell(6)]);cells[-2]['presentation_ms']=20
        long=audio_presentation_playlist(playlist(),40000)
        graph=dict(pgcs=[dict(cells=cells)])
        self.assertEqual(audio_presentations(graph,lambda name:long if name.endswith('00006.mpls') else playlist()),{1:40000})

    def test_updates_both_copies_without_touching_media(self):
        graph=dict(pgcs=[dict(cells=[dict(playlist=36,duration=3600000,audio_streams=[0])])])
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);media=root/'BDMV/STREAM/00036.m2ts';media.parent.mkdir(parents=True);media.write_bytes(b'original audio and video')
            for prefix in ('','BACKUP/'):
                path=root/('BDMV/'+prefix+'PLAYLIST/00036.mpls');path.parent.mkdir(parents=True);path.write_bytes(playlist())
            self.assertEqual(extend_audio_presentations(root,graph),{36:40000})
            self.assertEqual((root/'BDMV/PLAYLIST/00036.mpls').read_bytes(),(root/'BDMV/BACKUP/PLAYLIST/00036.mpls').read_bytes())
            self.assertEqual(playlist_times((root/'BDMV/PLAYLIST/00036.mpls').read_bytes()),(27000000,28800000))
            self.assertEqual(extend_audio_presentations(root,graph),{})
            self.assertEqual(media.read_bytes(),b'original audio and video')
