"""Support-file placement, independent resume and completed-media pruning."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dvd2uhd.iso import verify_manifest, support_directory
from dvd2hevc_app.uhd import prune_uhd_intermediates
from dvd2uhd.iso import create


class UhdSupportTests(unittest.TestCase):
    def test_writer_places_all_sidecars_inside_support_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'authored';(source/'BDMV/JAR').mkdir(parents=True)
            (source/'BDMV/index.bdmv').write_bytes(b'index')
            (source/'BDMV/JAR/00000.jar').write_bytes(b'jar')
            tool=root/'writer.exe';tool.write_bytes(b'writer');output=root/'movie.iso'
            def run(command,log,**kwargs):
                Path(log).write_text('log')
                if '--progress-file' in command:
                    Path(command[command.index('--progress-file')+1]).write_text('8')
                return 'UDF revision: 2.50' if 'info' in command else '0 errors'
            with patch('dvd2uhd.iso.run',side_effect=run),patch('dvd2uhd.iso.validate_udf',return_value={}),\
                 patch('dvd2uhd.iso.payload',return_value=[]):
                create(source,output,tool=tool,writer_progress=lambda done,total:None)
            self.assertEqual({p.name for p in root.iterdir()},
                             {'authored','writer.exe','movie.iso','Conversion files'})
            self.assertEqual({p.name for p in support_directory(output).iterdir()},
                             {'creation.log','verification.log','info.log','manifest.json','writer.progress'})

    def test_manifest_resume_checks_inventory_size_and_every_payload_byte(self):
        with tempfile.TemporaryDirectory() as temporary:
            image=Path(temporary)/'movie.iso';image.write_bytes(b'headerpayloadtail')
            rows=[dict(path='BDMV/STREAM/00000.m2ts',size=7,
                       sha256=hashlib.sha256(b'payload').hexdigest())]
            class Reader:
                files={'BDMV/STREAM/00000.m2ts':dict(size=7,extents=[(6,7)])}
            with patch('dvd2uhd.iso.UdfImage',return_value=Reader()):
                self.assertEqual(verify_manifest(image,rows),rows)
                image.write_bytes(b'headercorrupttail')
                with self.assertRaisesRegex(ValueError,'payload mismatch'):verify_manifest(image,rows)
                with self.assertRaisesRegex(ValueError,'inventory'):verify_manifest(image,[])
                with self.assertRaisesRegex(ValueError,'inventory'):verify_manifest(image,rows+rows)
                with self.assertRaisesRegex(ValueError,'size mismatch'):
                    verify_manifest(image,[dict(rows[0],size=8)])

    def test_shared_support_root_avoids_long_names_and_disc_collisions(self):
        root=Path(tempfile.gettempdir())
        support=root/'Conversion files'
        first=support_directory(root/'season 1'/'movie.iso',support)
        second=support_directory(root/'season 2'/'movie.iso',support)
        self.assertEqual(first.parent,support.resolve())
        self.assertNotEqual(first,second)
        self.assertEqual(len(first.name),16)

    def test_only_verified_owned_attempt_media_is_pruned(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();work=root/'work';work.mkdir()
            folder=work/'bdmv-0123456789ab';folder.mkdir()
            cache=work/(folder.name+'.work');cache.mkdir()
            output=root/'movie.iso';output.write_bytes(b'verified')
            (folder/'clip.m2ts').write_bytes(b'clip');(cache/'source.vob').write_bytes(b'source')
            (folder/'navigation.json').write_text('{}')
            (cache/'do-not-delete.iso').write_bytes(b'iso')
            other=work/'bdmv-aaaaaaaaaaaa';other.mkdir();(other/'clip.m2ts').write_bytes(b'other')
            receipt=dict(passed=False,output=str(output),folder=str(folder),
                         manifest=dict(verified=True,bytes=8),stock_vlc_gate=dict(passed=True))
            self.assertEqual(prune_uhd_intermediates(work,receipt)['files_removed'],0)
            receipt['passed']=True
            self.assertEqual(prune_uhd_intermediates(work,receipt)['files_removed'],2)
            self.assertTrue((folder/'navigation.json').is_file())
            self.assertEqual((cache/'do-not-delete.iso').read_bytes(),b'iso')
            self.assertEqual((other/'clip.m2ts').read_bytes(),b'other')
            self.assertEqual(output.read_bytes(),b'verified')
            receipt['folder']=str(other.parent.parent)
            self.assertEqual(prune_uhd_intermediates(work,receipt)['files_removed'],0)
