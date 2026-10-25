"""Menu repairs preserve DVD timing, physical reuse and compressed media."""
import os
from pathlib import Path
import tempfile
import unittest

from dvd2uhd.menu_pacing import repair_candidates


class MenuPacingTests(unittest.TestCase):
    def test_shared_menu_audio_uses_longer_ifo_or_native_interval(self):
        def cell(i, millis):
            return dict(clip=i, playlist=i, duration=millis*90)
        graph=dict(pgcs=[dict(domain='menu', cells=[cell(0,40000),cell(1,40),cell(2,40000)]),
                         dict(domain='menu', cells=[cell(0,40000)]),
                         dict(domain='title',cells=[cell(3,40000)])])
        report=dict(cells=[dict(index=i, menus=int(i!=3), audio_streams=[0] if i!=2 else [],
                               playlist_times=[27000000,27000000+(30000 if i==1 else 20)*45])
                           for i in range(4)])
        files=[dict(path=f'BDMV/STREAM/{i:05d}.m2ts',size=1000000) for i in range(4)]
        result=repair_candidates(graph,report,files)
        self.assertEqual([(r['clip'],r['millis']) for r in result],[(0,40000),(1,30000)])
        files[0]['size']=20000000
        self.assertEqual([r['clip'] for r in repair_candidates(graph,report,files)],[1])
        report['cells'][3]['menus']=1
        self.assertEqual([r['clip'] for r in repair_candidates(graph,report,files)],[1,3])

    @unittest.skipUnless(os.environ.get('DVD2HEVC_RUN_MUX_TESTS'), 'Optional real FFmpeg/tsMuxeR integration')
    def test_real_mux_paces_menu_without_changing_video_or_audio(self):
        from dvd2uhd.author import run,mux_cell,decoded_frame_hashes,probe_media
        from dvd2uhd.audit import audio_hash,packet_timing_hash
        ff=os.environ['DVD2HEVC_TEST_FFMPEG']
        probe=os.environ['DVD2HEVC_TEST_FFPROBE']
        mux=os.environ['DVD2HEVC_TEST_TSMUXER']
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source.vob'
            run([ff,'-nostdin','-v','error','-y','-f','lavfi','-i',
                 'testsrc2=size=160x120:rate=25:duration=0.08','-f','lavfi','-i',
                 'sine=frequency=440:sample_rate=48000:duration=12',
                 '-c:v','mpeg2video','-bf','0','-c:a','ac3','-f','vob',source])
            expected=decoded_frame_hashes(source,ff,root/'source-frames.log')
            streams,_=probe_media(source,probe)
            original_audio=next(s for s in streams['streams'] if s['codec_type']=='audio')
            digest=audio_hash(source,original_audio['index'],ff)
            sizes=[];packet_timing=[]
            for interactive in (False,True):
                folder=root/str(interactive);folder.mkdir()
                (folder/'source.vob').write_bytes(source.read_bytes())
                output=folder/'output'
                mux_cell(folder,0,output,ff,probe,mux,duration_ticks=1080000,interactive=interactive)
                media=output/'BDMV/STREAM/00000.m2ts';sizes.append(media.stat().st_size)
                self.assertEqual(decoded_frame_hashes(media,ff,folder/'frames.log'),expected)
                streams,_=probe_media(media,probe)
                track=next(s for s in streams['streams'] if s['codec_type']=='audio')
                self.assertEqual(audio_hash(media,track['index'],ff),digest)
                packet_timing.append(packet_timing_hash(media,probe,folder/'packet-probe.log'))
            self.assertLess(sizes[0],1000000)
            self.assertGreater(sizes[1],5500000)
            self.assertEqual(packet_timing[0],packet_timing[1])
