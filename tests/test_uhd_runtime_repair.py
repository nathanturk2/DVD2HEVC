"""Runtime repairs must preserve resources, allocation and every other ISO byte."""
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from dvd2uhd.runtime_repair import repaired_jar,patch_image,RuntimeAllocationError


class RuntimeRepairTests(unittest.TestCase):
    entry='org/dvd2uhd/DvdXlet.class'

    def jar(self,padding=4096):
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(self.entry,b'\xca\xfe\xba\xbeoriginal-class')
            archive.writestr('graphics/00000.gfx',b'original artwork')
            archive.comment=b' '*padding
        return stream.getvalue()

    def test_equal_allocation_and_untouched_artwork(self):
        old=self.jar();replacement=b'\xca\xfe\xba\xbeupdated-class'
        fixed,record=repaired_jar(old,{self.entry:replacement})
        self.assertEqual(len(fixed),len(old))
        with zipfile.ZipFile(io.BytesIO(fixed)) as archive:
            self.assertEqual(archive.read(self.entry),replacement)
            self.assertEqual(archive.read('graphics/00000.gfx'),b'original artwork')
        self.assertEqual(record['changed_entries'],[self.entry])

    def test_bad_class_missing_entry_signed_jar_and_growth_are_rejected(self):
        for replacements in ({self.entry:b'bad'},{'missing.class':b'\xca\xfe\xba\xbeclass'}):
            with self.assertRaises(ValueError):repaired_jar(self.jar(),replacements)
        stream=io.BytesIO(self.jar())
        with zipfile.ZipFile(stream,'a') as archive:archive.writestr('META-INF/A.SF',b'signed')
        with self.assertRaisesRegex(ValueError,'Signed'):repaired_jar(stream.getvalue(),{self.entry:b'\xca\xfe\xba\xbefixed'})
        with self.assertRaises(RuntimeAllocationError):repaired_jar(self.jar(0),{self.entry:b'\xca\xfe\xba\xbe'+os.urandom(16384)})

    def fixture(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name);source=root/'original.iso';candidate=root/'fixed.iso';jar=self.jar()
        data=bytearray(b'unchanged video/audio'*500000);split=len(jar)//2
        spans=[(8*1024**2-32,split),(len(data)-len(jar),len(jar)-split)]
        at=0
        for offset,length in spans:data[offset:offset+length]=jar[at:at+length];at+=length
        source.write_bytes(data);files={'BDMV/JAR/00000.jar':dict(size=len(jar),extents=spans)}
        class Reader:
            def __init__(self,path):self.path=path;self.files=files
            def read(self,name,limit=None):
                with Path(self.path).open('rb') as stream:
                    parts=[];remaining=files[name]['size']
                    for offset,length in files[name]['extents']:
                        stream.seek(offset);block=stream.read(min(length,remaining));parts.append(block);remaining-=len(block)
                return b''.join(parts)
        context=patch('dvd2uhd.udf_reader.UdfImage',Reader);context.start();self.addCleanup(context.stop)
        context=patch('dvd2uhd.udf_validation.validate_udf',return_value={'verified':True});context.start();self.addCleanup(context.stop)
        return source,candidate,bytes(data)

    def test_fragmented_jar_crossing_copy_boundaries_preserves_every_other_byte(self):
        source,candidate,original=self.fixture()
        result=patch_image(source,candidate,{self.entry:b'\xca\xfe\xba\xbefixed'})
        self.assertEqual(source.read_bytes(),original)
        self.assertTrue(result['all_non_jar_bytes_identical'])
        self.assertEqual(result['image_sha256'],hashlib.sha256(candidate.read_bytes()).hexdigest())
        self.assertNotEqual(source.read_bytes(),candidate.read_bytes())
        self.assertEqual(candidate.stat().st_size,source.stat().st_size)

    def test_candidate_corruption_and_source_changes_never_pass(self):
        for modify_source in (False,True):
            source,candidate,original=self.fixture();changed=False
            def progress(phase,completed,total):
                nonlocal changed
                if phase=='uhd-runtime-copy' and completed==total and not changed:
                    changed=True
                    with (source if modify_source else candidate).open('r+b') as stream:stream.write(b'X')
            with self.assertRaises(ValueError):patch_image(source,candidate,{self.entry:b'\xca\xfe\xba\xbefixed'},progress)
            if not modify_source:self.assertEqual(source.read_bytes(),original)

    def test_jar_allocation_cannot_alias_another_payload(self):
        source,candidate,original=self.fixture()
        from dvd2uhd.udf_reader import UdfImage
        files=UdfImage(source).files
        files['BDMV/STREAM/00000.m2ts']=dict(size=16,extents=files['BDMV/JAR/00000.jar']['extents'][:1])
        with self.assertRaisesRegex(ValueError,'aliases'):
            patch_image(source,candidate,{self.entry:b'\xca\xfe\xba\xbefixed'})
        self.assertFalse(candidate.exists())
        self.assertEqual(source.read_bytes(),original)

    def test_mutation_after_masked_verification_is_rejected(self):
        source,candidate,original=self.fixture()
        def progress(phase,completed,total):
            if phase=='uhd-runtime-verify' and completed==total:
                with candidate.open('r+b') as stream:stream.write(b'X')
        with self.assertRaisesRegex(ValueError,'candidate changed'):
            patch_image(source,candidate,{self.entry:b'\xca\xfe\xba\xbefixed'},progress)
        self.assertEqual(source.read_bytes(),original)

    def test_native_still_metadata_and_jar_patch_preserve_all_other_bytes(self):
        from test_uhd_native_still import playlist
        from dvd2uhd.bdmv import native_still_playlist
        source,candidate,_=self.fixture()
        from dvd2uhd.udf_reader import UdfImage
        files=UdfImage(source).files;metadata=playlist()
        for offset,prefix in ((4096,''),(8192,'BACKUP/')):
            files['BDMV/'+prefix+'PLAYLIST/00036.mpls']=dict(size=len(metadata),extents=[(offset,len(metadata))])
            with source.open('r+b') as stream:stream.seek(offset);stream.write(metadata)
        files['BDMV/STREAM/00036.m2ts']=dict(size=128,extents=[(12288,128)])
        original=source.read_bytes()
        result=patch_image(source,candidate,{self.entry:b'\xca\xfe\xba\xbefixed'},native_still_playlists=[36])
        self.assertFalse(result['all_non_jar_bytes_identical'])
        self.assertTrue(result['all_unmodified_bytes_identical'] and result['media_bytes_identical'])
        self.assertEqual(len(result['changed_files']),3)
        self.assertEqual(source.read_bytes(),original)
        self.assertEqual(UdfImage(candidate).read('BDMV/PLAYLIST/00036.mpls'),native_still_playlist(metadata))
        self.assertEqual(candidate.read_bytes()[12288:12416],original[12288:12416])


if __name__=='__main__':unittest.main()
