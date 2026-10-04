import io
import struct
import unittest
import gzip,tempfile,zlib
from pathlib import Path
from dvd2uhd.ifo import Reader,pgc,dvd_time
from dvd2uhd.pes import pts,pci,packets,cell_blocks
from dvd2uhd.spu import decode,rgba,colour_changes
from dvd2uhd.source import FormatError
from dvd2uhd.bdmv import bdjo,index_from_mux,rename_playlist,joined_playlist,playlist_times,section,u16,u32
from dvd2uhd.author import unique_cells
from dvd2uhd.binary import graphics,Writer


class PictureAuditTests(unittest.TestCase):
    def test_still_padding_cannot_hide_missing_or_changed_original_pictures(self):
        from dvd2uhd.audit import picture_payload_matches
        self.assertFalse(picture_payload_matches(['a','b'],['a'],True))
        self.assertFalse(picture_payload_matches([],[],True))
        self.assertFalse(picture_payload_matches(['a','b'],['a','c','a'],True))
        self.assertTrue(picture_payload_matches(['a','b'],['a','b','a'],True))
        self.assertTrue(picture_payload_matches(['a','b'],['a','b']))
        self.assertFalse(picture_payload_matches(['a','b'],['a','b','a']))


class IfoTests(unittest.TestCase):
    def test_bounds_checked(self):
        with self.assertRaises(FormatError):Reader(b"1234").n(3)

    def test_bcd_pal_and_ntsc(self):
        self.assertEqual(dvd_time(bytes([0,1,2,0x40|0x12])),5623200)
        self.assertEqual(dvd_time(bytes([0,0,1,0xc0|0x15])),135045)

    def test_command_and_cell_offsets(self):
        data=bytearray(512)
        data[2:4]=bytes([1,1])
        struct.pack_into(">HHHH",data,228,236,260,264,288)
        struct.pack_into(">HHH",data,236,1,1,0)
        data[244:252]=bytes.fromhex("71000000002a0000")
        data[252:260]=bytes.fromhex("3002000000010000")
        data[260]=1;data[266]=255;data[267]=1
        struct.pack_into(">IIII",data,272,3,0,6,7)
        struct.pack_into(">HBB",data,288,2,0,4)
        p=pgc(Reader(bytes(data)),0,"T:1:1")
        self.assertEqual(p["pre"],["71000000002a0000"])
        self.assertEqual(p["post"],["3002000000010000"])
        self.assertEqual((p["cells"][0]["first"],p["cells"][0]["last"]),(3,7))
        self.assertEqual(p["cells"][0]["cell_id"],4)
        self.assertEqual(p["cells"][0]["still"],255)

class AudioFrameTests(unittest.TestCase):
    def test_bounded_edge_probe_matches_full_inventory_with_false_sync_inside_payload(self):
        from dvd2uhd.boundary_audio import edge_fragments
        from dvd2uhd.audio_frames import inventory
        frame=bytes.fromhex('0b7700000040')+bytes(range(122))
        frame=frame[:35]+bytes.fromhex('0b7700000040')+frame[41:]
        raw=frame[71:]+frame*600+frame[:39]
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'audio.ac3';path.write_bytes(raw);prefix,suffix=edge_fragments(path)
        full=inventory([raw]);self.assertEqual(prefix,frame[71:]);self.assertEqual(suffix,frame[:39])
        self.assertEqual((len(prefix),len(suffix)),(full['prefix_bytes'],full['suffix_bytes']))

    def test_split_frame_completion_preserves_shared_clip_and_rejects_conflicting_branches(self):
        from dvd2uhd.boundary_audio import plan,complete
        from dvd2uhd.audio_frames import inventory
        frame=bytes.fromhex('0b7700000040')+bytes(range(122))
        cells=[dict(clip=i,flags=8,still=0,command=0) for i in range(3)]
        p=dict(domain='title',still=0,cells=cells[:2]);g=dict(pgcs=[p])
        rows=[dict(audio_streams=[0],audio_formats=[('ac3','48000',2)]) for _ in range(3)]
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            for i,payload in enumerate([frame+frame[:16],frame[16:]+frame,frame[16:]+frame]):
                folder=root/f'cell-{i:05d}';folder.mkdir();(folder/'audio-00.ac3').write_bytes(payload)
            repairs,unresolved=plan(g,dict(cells=rows),root)
            self.assertEqual(list(repairs),[0]);self.assertEqual(unresolved,[])
            path=root/'cell-00000/audio-00.ac3';complete(path,repairs[0][0])
            self.assertEqual(path.read_bytes(),frame*2)
            self.assertEqual(inventory([path.read_bytes()])['frames'],2)
            path.write_bytes(frame+frame[:16]);g['pgcs'].append(dict(domain='title',still=0,cells=[cells[0],cells[2]]))
            repairs,unresolved=plan(g,dict(cells=rows),root)
            self.assertEqual(repairs,{})
            self.assertEqual(unresolved[0]['reason'],'ambiguous-or-nonseamless-successor')

    def test_complete_frames_and_boundary_fragments_are_distinct(self):
        from dvd2uhd.audio_frames import inventory
        frame=bytes.fromhex('0b7700000040')+bytes(range(122))
        raw=b'\x78\x00\xfd\x65'+frame+frame+frame[:17]
        result=inventory(raw[i:i+7] for i in range(0,len(raw),7))
        clean=inventory([frame+frame])
        self.assertEqual((result['frames'],result['frame_bytes'],result['prefix_bytes'],result['suffix_bytes']),(2,256,4,17))
        self.assertEqual(result['frames_sha256'],clean['frames_sha256'])
        self.assertNotEqual(result['payload_sha256'],clean['payload_sha256'])
        with self.assertRaises(FormatError):inventory([frame+b'junkjunk'+frame])

class SubtitleControlTests(unittest.TestCase):
    def test_vts_inventory_keeps_logical_ordinals_and_ignores_unused_padding(self):
        from dvd2uhd.subtitle_controls import languages
        a=dict(domain='title',vts=1,subpicture=[0x80000000,0,0x80020200]+[0]*29)
        b=dict(domain='title',vts=1,subpicture=[0,0x80010100]+[0]*30)
        graph=dict(pgcs=[a,b],domains={'T:1':dict(subtitles=[dict(ordinal=i,language=lang) for i,lang in enumerate(['en','iw','nl']+['\0\0']*29)])})
        self.assertEqual(languages(graph,[(b,{})]),['eng','heb','nld'])
        self.assertEqual(languages(graph,[(dict(domain='menu',vts=1),{})]),[])
        graph['domains']['T:1']['subtitles']=graph['domains']['T:1']['subtitles'][:2]
        with self.assertRaises(ValueError):languages(graph,[(a,{})])

    def test_clear_channels_contain_no_objects_or_opaque_palette_entries(self):
        from dvd2uhd.subtitle_controls import clear_stream
        raw=clear_stream(24000/1001);pos=0;segments=[]
        while pos<len(raw):
            self.assertEqual(raw[pos:pos+2],b'PG')
            pts,dts,kind,size=struct.unpack_from('>IIBH',raw,pos+2)
            data=raw[pos+13:pos+13+size];self.assertEqual(len(data),size)
            segments.append((pts,kind,data));pos+=13+size
        self.assertEqual([s[1] for s in segments],[0x16,0x17,0x14,0x80]*2)
        self.assertEqual([s[0] for s in segments],[0]*4+[900]*4)
        for stamp,kind,data in segments:
            if kind==0x16:
                self.assertEqual(struct.unpack_from('>HHB',data),(1920,1080,0x10))
                self.assertEqual(data[10],0) # composition object count: draw nothing
            if kind==0x14:self.assertEqual(data[-1],0) # transparent palette
        with self.assertRaises(ValueError):clear_stream(12)

    def test_mpls_native_languages_are_bounded_and_preserve_stream_order(self):
        from dvd2uhd.bdmv import primary_streams
        def entry(pid,attrs):return bytes([3,1])+u16(pid)+bytes([len(attrs)])+attrs
        stn=u16(14+8+10+9*2)+b'\0\0'+bytes([1,1,2])+b'\0'*9
        stn+=entry(4113,b'\x24\x60\0')+entry(4352,b'\x81\x31eng')
        stn+=entry(4608,b'\x90eng')+entry(4609,b'\x90deu')
        body=b'00000M2TS'+b'\0\x01'+b'\0'+u32(90000)+u32(135000)+b'\0'*12+stn
        data=bytearray(40);data[:8]=b'MPLS0300';struct.pack_into('>I',data,8,40)
        data+=section(b'\0\0'+u16(1)+u16(0)+u16(len(body))+body)
        table=primary_streams(data)[0]
        self.assertEqual([s['language'] for s in table['subtitle']],['eng','deu'])
        self.assertEqual([s['pid'] for s in table['subtitle']],[4608,4609])
        with self.assertRaises(ValueError):primary_streams(data[:-1])

class SpuTests(unittest.TestCase):
    def picture(self):
        # 4x2, colour 1 top and 2 bottom, terminated at 1024 DVD ticks.
        controls=bytes.fromhex("000000060103321004fff0050000030000010600040005ff")
        raw=bytearray(b"\0\0\0\6\x11\x12"+controls)
        struct.pack_into(">H",raw,0,len(raw))
        return bytes(raw)

    def test_interlaced_rle(self):
        p=decode(self.picture(),90000)[0]
        self.assertEqual(p.pixels,bytes([1]*4+[2]*4))
        self.assertEqual((p.width,p.height),(4,2))

    def test_highlight_alpha_and_mask(self):
        p=decode(self.picture(),90000)[0]
        palette=[0x108080,0xeb8080]+[0]*14
        img=rgba(p,palette,0x1111ffff)
        self.assertEqual(img.getpixel((0,0)),(255,255,255,255))

    def test_broken_control_chain_rejected(self):
        raw=bytearray(self.picture());raw[8:10]=b"\xff\xff"
        with self.assertRaises(FormatError):decode(bytes(raw))

    def test_compact_mask_preserves_exact_pixels(self):
        p=decode(self.picture(),90000)[0]
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'mask.gfx';graphics([],[(0,0,p)],0,path)
            raw=gzip.decompress(path.read_bytes())
        self.assertEqual(raw[:4],b'GXF4')
        # Header, stream/times/forced/rectangle, color and alpha arrays.
        offset=12+37+2*(4+4*4)
        size=int.from_bytes(raw[offset:offset+4],'big')
        self.assertEqual(zlib.decompress(raw[offset+4:offset+4+size]),p.pixels)

    def test_invisible_spu_retains_clear_event(self):
        raw=bytes.fromhex('000a00040000000402ff')
        events=[]
        self.assertEqual(decode(raw,events=events),[])
        self.assertEqual(events,[0])

    def test_colcon_changes_are_bounded_and_timed(self):
        raw=bytearray(self.picture())
        # New sequence at 1024 ticks: white only on top row columns 1..2,
        # transparent from column 3. The lower field retains its base colour.
        parameter=bytes.fromhex('00160000200000011111ffff0003111100000fffffff')
        end=len(raw)
        struct.pack_into('>H',raw,8,end)
        raw.extend(struct.pack('>HH',1,end)+b'\x07'+parameter+b'\xff')
        struct.pack_into('>H',raw,0,len(raw))
        pictures=decode(bytes(raw),90000)
        self.assertEqual([(p.start,p.end) for p in pictures],[(0,1024),(1024,90000)])
        self.assertEqual(pictures[0].colcon,[])
        self.assertEqual(pictures[1].pixels,pictures[0].pixels)
        img=rgba(pictures[1],[0x108080,0xeb8080]+[0x108080]*14)
        self.assertEqual(img.getpixel((1,0)),(255,255,255,255))
        self.assertEqual(img.getpixel((3,0))[3],0)
        self.assertEqual(img.getpixel((1,1))[:3],(0,0,0))
        # A PCI highlight overrides the SPU's regional palette/alpha.
        self.assertEqual(rgba(pictures[1],[0x108080,0xeb8080]+[0]*14,0x1111ffff).getpixel((3,0)),(255,255,255,255))

    def test_colcon_multiple_bands_and_invalid_lengths(self):
        parameter=bytes.fromhex('001a0000100000001111ffff0001100100022222ffff0fffffff')
        self.assertEqual(colour_changes(parameter),[(0,0,4095,0,0x1111ffff),(2,1,4095,1,0x2222ffff)])
        for bad in (parameter[:-1],bytes.fromhex('000a000010000fffffff'),
                    bytes.fromhex('000600000000'),bytes.fromhex('00060fffffff00')):
            with self.assertRaises(FormatError):colour_changes(bad)

class BranchingTests(unittest.TestCase):
    def sector(self,vob,cell,extent=0):
        b=bytearray(2048);b[:4]=bytes.fromhex('000001ba')
        p=bytearray(1018);p[0]=1
        struct.pack_into('>I',p,9,extent);struct.pack_into('>H',p,25,vob);p[28]=cell
        b[14:20]=bytes.fromhex('000001bf')+len(p).to_bytes(2,'big');b[20:20+len(p)]=p
        return bytes(b)

    def test_interleaved_branch_identity_excludes_other_cut(self):
        a=self.sector(3,1);b=self.sector(4,1);c=self.sector(3,1)
        self.assertEqual(b''.join(cell_blocks([a+b+c],(3,1))),a+c)

    def test_partial_vobu_rejected(self):
        with self.assertRaises(FormatError):list(cell_blocks([self.sector(3,1,2)],(3,1)))

    def test_shared_clip_is_one_asset(self):
        c=dict(first=10,last=20,interleaved=False)
        g=dict(pgcs=[dict(vts=1,domain='title',cells=[c]),dict(vts=1,domain='title',cells=[dict(c)])])
        cells=unique_cells(g)
        self.assertEqual(len(cells),1);self.assertEqual(len(next(iter(cells.values()))),2)

    def test_same_interleaved_range_different_identity_stays_separate(self):
        c=dict(first=10,last=20,interleaved=True,vob_id=1,cell_id=1)
        d=dict(c,vob_id=2)
        g=dict(pgcs=[dict(vts=1,domain='title',cells=[c,d])])
        self.assertEqual(len(unique_cells(g)),2)

class ContainerTests(unittest.TestCase):
    def test_still_asset_stays_bounded_and_audio_tail_is_silent(self):
        from dvd2uhd.author import still_padding_seconds
        self.assertEqual(still_padding_seconds(3600,254,False,0),61.04)
        self.assertEqual(still_padding_seconds(3600,10,False,0),11.04)
        self.assertEqual(still_padding_seconds(3600,0,True,0),61)
        self.assertEqual(still_padding_seconds(21657600,0,True,240.608),241.64)
        self.assertEqual(still_padding_seconds(3600,0,True,120),121)
        from dvd2uhd.author import still_silent_tail_ms
        probe=dict(streams=[dict(codec_type='video',start_time='100'),dict(codec_type='audio',start_time='100.1',duration='59.2')])
        self.assertEqual(still_silent_tail_ms(probe,[27000000,29745000]),1700)
        del probe['streams'][1]['duration']
        self.assertEqual(still_silent_tail_ms(probe,[27000000,29745000]),0)
    def test_java_modified_utf_preserves_undefined_language_codes(self):
        w=Writer();w.text('M:0:\xff\x00:1')
        self.assertEqual(w.f.getvalue(),b'\x00\x0aM:0:\xc3\xbf\xc0\x80:1')
        w=Writer();w.text('\U0001f600')
        self.assertEqual(w.f.getvalue(),b'\x00\x06\xed\xa0\xbd\xed\xb8\x80')
    def playlist(self,clip,begin,end):
        item=f'{clip:05d}'.encode()+b'M2TS'+b'\0\1\0'+u32(begin)+u32(end)+b'\0'*16
        body=section(b'\0\0'+u16(1)+u16(0)+u16(len(item))+item)
        header=bytearray(64);header[:8]=b'MPLS0300';struct.pack_into('>II',header,8,64,64+len(body))
        return bytes(header)+body+section(u16(0))

    def test_join_preserves_clip_order_reuse_and_chapter_positions(self):
        a=self.playlist(7,90000,135000);b=self.playlist(9,180000,270000)
        joined,times=joined_playlist([a,b,a],[1,3])
        start,marks=struct.unpack_from('>II',joined,8)
        self.assertEqual(int.from_bytes(joined[start+6:start+8],'big'),3)
        self.assertEqual(times,[(90000,135000),(180000,270000),(90000,135000)])
        self.assertEqual(int.from_bytes(joined[marks+4:marks+6],'big'),2)
        self.assertEqual(struct.unpack_from('>HI',joined,marks+6+14+2),(2,90000))
        self.assertEqual(joined.count(b'00007M2TS'),2)

    def test_bdjo_addresses(self):
        b=bdjo();self.assertEqual(b[:8],b"BDJO0200")
        addresses=struct.unpack_from(">6I",b,8)
        self.assertEqual(addresses[0],48)
        self.assertEqual(addresses,tuple(sorted(addresses)))
        for i in range(4):self.assertEqual(addresses[i+1],addresses[i]+4+int.from_bytes(b[addresses[i]:addresses[i]+4],"big"))

    def test_menu_movie_services_keep_one_persistent_vm(self):
        b=bdjo();management=int.from_bytes(b[20:24],'big')
        descriptor=management+4+2+18
        self.assertEqual(b[descriptor+8]&128,0)  # Not service-bound.
        template=bytearray(64);template[:8]=b'INDX0300';struct.pack_into('>I',template,8,64)
        template.extend(section(b'\0'*38))
        index=index_from_mux(template);start=int.from_bytes(index[8:12],'big')+4
        self.assertEqual([int.from_bytes(index[start+n+4:start+n+6],'big')>>14 for n in (0,12,26)],[3,3,2])
        self.assertEqual(int.from_bytes(index[start+24:start+26],'big'),1)
        self.assertEqual([index[start+n+6:start+n+11] for n in (0,12,26)],[b'00000']*3)


    def test_seamless_connection_has_distinct_stc_condition(self):
        source=self.playlist(7,90000,135000)
        data,_=joined_playlist([source,source],[1],[1,5])
        start=int.from_bytes(data[8:12],'big');pos=start+10
        self.assertEqual(data[pos+12]&15,1)
        pos+=2+int.from_bytes(data[pos:pos+2],'big')
        self.assertEqual(data[pos+12]&15,5)

    def test_join_uses_dvd_duration_to_prevent_cumulative_drift(self):
        source=self.playlist(7,90000,135000)
        data,times=joined_playlist([source,source],[1,2],[1,5],[97200,97200])
        self.assertEqual(times,[(90000,138600),(90000,138600)])
        start=int.from_bytes(data[8:12],'big');pos=start+10
        self.assertEqual(int.from_bytes(data[pos+18:pos+22],'big'),138600)

    def test_control_cell_does_not_disable_movie_join_and_runs_keep_chapters(self):
        from dvd2uhd.bdmv import join_titles
        rows=[dict(playlist_times=[90000,135000],video_codec='hevc',sequence_end=True) for _ in range(5)]
        cells=[dict(number=i+1,clip=i,playlist=i,duration=90000,flags=8,still=0,command=0) for i in range(5)]
        cells[2]['still']=255
        pgc=dict(key='T:1:1',domain='title',still=0,cells=cells,programs=[1,2,4,5])
        report=dict(cells=rows)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'BDMV/PLAYLIST').mkdir(parents=True);(root/'BDMV/BACKUP/PLAYLIST').mkdir(parents=True)
            for i in range(5):(root/'BDMV/PLAYLIST'/f'{i:05d}.mpls').write_bytes(self.playlist(i,90000,135000))
            joined=join_titles(root,dict(pgcs=[pgc]),report)
        self.assertEqual([j['cells'] for j in joined],[[1,2],[4,5]])
        self.assertEqual([j['chapter_cells'] for j in joined],[[1,2],[4,5]])
        self.assertEqual([c['playlist'] for c in cells],[5,5,2,6,6])
        self.assertEqual(cells[3]['start_ms'],0)
        self.assertEqual(cells[4]['end_ms'],2000)

    def test_timed_black_tail_keeps_native_last_chapter(self):
        from dvd2uhd.bdmv import join_titles
        rows=[dict(playlist_times=[90000,135000],video_codec='hevc',sequence_end=True) for _ in range(3)]
        cells=[dict(number=i+1,clip=i,playlist=i,duration=90000,flags=8,still=0,command=0) for i in range(3)]
        cells[-1].update(still_repeated=True,duration=43200)
        pgc=dict(key='T:1:1',domain='title',still=0,cells=cells,programs=[1,3])
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'BDMV/PLAYLIST').mkdir(parents=True);(root/'BDMV/BACKUP/PLAYLIST').mkdir(parents=True)
            for i in range(3):(root/'BDMV/PLAYLIST'/f'{i:05d}.mpls').write_bytes(self.playlist(i,90000,135000))
            joined=join_titles(root,dict(pgcs=[pgc]),dict(cells=rows))
        self.assertEqual(joined[0]['chapter_cells'],[1,3])
        self.assertEqual(cells[-1]['end_ms'],2480)

    def test_cell_end_command_finishes_run_instead_of_excluding_movie_cell(self):
        from dvd2uhd.bdmv import join_titles
        rows=[dict(playlist_times=[90000,135000],video_codec='hevc',sequence_end=True) for _ in range(5)]
        cells=[dict(number=i+1,clip=i,playlist=i,duration=90000,flags=8,still=0,command=0) for i in range(5)]
        cells[1]['command']=1
        pgc=dict(key='T:1:1',domain='title',still=0,cells=cells,programs=[1,2,3,5])
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'BDMV/PLAYLIST').mkdir(parents=True);(root/'BDMV/BACKUP/PLAYLIST').mkdir(parents=True)
            for i in range(5):(root/'BDMV/PLAYLIST'/f'{i:05d}.mpls').write_bytes(self.playlist(i,90000,135000))
            joined=join_titles(root,dict(pgcs=[pgc]),dict(cells=rows))
        self.assertEqual([j['cells'] for j in joined],[[1,2],[3,4,5]])
        self.assertEqual([j['chapter_cells'] for j in joined],[[1,2],[3,5]])
        self.assertEqual(cells[1]['command'],1)
        self.assertEqual(cells[1]['end_ms'],2000)
        self.assertNotEqual(cells[1]['playlist'],cells[2]['playlist'])

if __name__=="__main__":unittest.main()
