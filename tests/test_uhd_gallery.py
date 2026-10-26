import copy
import os
from pathlib import Path
import struct
import tempfile
import unittest
from dvd2uhd.bdmv import section,u16,u32
from dvd2uhd.gallery import plans,install,verify


class GalleryTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('DVD2HEVC_RUN_MUX_TESTS'),'Optional real FFmpeg still extension')
    def test_two_different_pictures_hold_the_final_frame_losslessly_and_copy_audio_once(self):
        from dvd2uhd.author import run,decoded_frame_hashes,freeze_final_picture,probe_media
        from dvd2uhd.audit import audio_hash
        ff=os.environ['DVD2HEVC_TEST_FFMPEG'];probe=os.environ['DVD2HEVC_TEST_FFPROBE']
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);source=root/'two-pictures.mkv';output=root/'held.mkv'
            run([ff,'-nostdin','-v','error','-y','-f','lavfi','-i','testsrc2=size=160x120:rate=25:duration=0.08',
                 '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=0.4',
                 '-c:v','libx265','-preset','ultrafast','-x265-params','lossless=1:log-level=error:bframes=0',
                 '-c:a','ac3',source])
            hashes=decoded_frame_hashes(source,ff,root/'source.log')
            self.assertEqual(len(hashes),2);self.assertNotEqual(hashes[0],hashes[1])
            record=freeze_final_picture(source,output,ff,4,25,hashes,root)
            actual=decoded_frame_hashes(output,ff,root/'output.log',101)
            self.assertEqual(len(actual),100);self.assertEqual(actual[:2],hashes)
            self.assertEqual(set(actual[2:]),{hashes[-1]});self.assertEqual(record['output_pictures'],100)
            a,_=probe_media(source,probe);b,_=probe_media(output,probe)
            ai=next(s['index'] for s in a['streams'] if s['codec_type']=='audio')
            bi=next(s['index'] for s in b['streams'] if s['codec_type']=='audio')
            self.assertEqual(audio_hash(source,ai,ff),audio_hash(output,bi,ff))

    def graph(self):
        def pgc(n,count,held):
            return dict(key=f'T:1:{n}',playback_mode=0,programs=list(range(1,count+1)),still=0,
                        cells=[dict(number=i+1,playlist=i%2,still=255 if held else 0,duration=43200,
                                    command=0) for i in range(count)])
        return dict(pgcs=[pgc(1,1,True),pgc(2,2,True)],
                    titles=[dict(number=1,vts=1,parts=[dict(pgc=p,program=n) for p,n in ((1,1),(2,1),(2,2))])])

    def playlist(self,n):
        item=f'{n:05d}'.encode()+b'M2TS'+b'\0\1\0'+u32(90000)+u32(90900)+b'\0'*9+b'\2\0\0'+b'\0'*4
        body=section(b'\0\0'+u16(1)+u16(0)+u16(len(item))+item)
        head=bytearray(64);head[:8]=b'MPLS0300';struct.pack_into('>II',head,8,64,64+len(body))
        return bytes(head)+body+section(u16(0))

    def test_native_chapters_keep_original_ptt_order_and_guard_held_packet_boundaries(self):
        graph=self.graph();original=copy.deepcopy(graph)
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)
            for prefix in ('','BACKUP/'):
                folder=root/f'BDMV/{prefix}PLAYLIST';folder.mkdir(parents=True)
                for n in range(2):(folder/f'{n:05d}.mpls').write_bytes(self.playlist(n))
            report={};rows=install(root,graph,report);row=rows[0]
            self.assertEqual(row['chapters'],3);self.assertEqual(row['clips'],[0,0,1])
            self.assertEqual(row['items'],[0,2,4]);self.assertEqual(row['guard_items'],[1,3,5])
            data=(root/'BDMV/PLAYLIST/00002.mpls').read_bytes()
            self.assertEqual(data,(root/'BDMV/BACKUP/PLAYLIST/00002.mpls').read_bytes())
            start,mark=struct.unpack_from('>II',data,8)
            self.assertEqual(int.from_bytes(data[start+6:start+8],'big'),6)
            self.assertEqual(int.from_bytes(data[mark+4:mark+6],'big'),3)
            self.assertEqual([int.from_bytes(data[mark+8+n*14:mark+10+n*14],'big') for n in range(3)],[0,2,4])
            cells=[c for p in graph['pgcs'] for c in p['cells']]
            self.assertEqual([c['native_item'] for c in cells],[0,2,4])
            self.assertEqual([c['start_ms'] for c in cells],[0,40,80])
            self.assertEqual(verify(root,graph,report),1)
            cells[1]['native_item']=1
            with self.assertRaises(ValueError):verify(root,graph,report)
            cells[1]['native_item']=2
            self.assertFalse((root/'BDMV/STREAM').exists())
            for n in range(2):self.assertEqual((root/f'BDMV/PLAYLIST/{n:05d}.mpls').read_bytes(),self.playlist(n))
        self.assertEqual(graph['titles'],original['titles'])
        for old,new in zip(original['pgcs'],graph['pgcs']):
            for a,b in zip(old['cells'],new['cells']):
                for key in ('duration','still','command'):self.assertEqual(a[key],b[key])

    def test_branching_or_incomplete_chapter_maps_keep_existing_vm_path(self):
        graph=self.graph();self.assertEqual(len(plans(graph)),1)
        shared=copy.deepcopy(graph);shared['titles'].append(dict(number=2,vts=1,parts=[dict(pgc=2,program=1)]))
        self.assertEqual(plans(shared),[])
        missing=copy.deepcopy(graph);missing['titles'][0]['parts'].pop(1);self.assertEqual(plans(missing),[])
        repeated=copy.deepcopy(graph);repeated['titles'][0]['parts'].append(dict(pgc=1,program=1))
        self.assertEqual(plans(repeated),[])
        ordinary=copy.deepcopy(graph)
        for p in ordinary['pgcs']:
            for c in p['cells']:c['still']=0
        self.assertEqual(plans(ordinary),[])


if __name__=='__main__':unittest.main()
