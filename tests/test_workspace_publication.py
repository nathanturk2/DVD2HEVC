import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from dvd2hevc_app.publication import publish_image
from dvd2hevc_app.workspace import configured_work_root,save_work_root,capacity_estimate,GIB
from dvd2hevc_app import frontend
from dvd2hevc_app.cli import build_parser


class WorkspacePublicationTests(unittest.TestCase):
    def fixture(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name);source=root/'work/image.part';source.parent.mkdir();source.write_bytes(b'\0content'*1000)
        return root,source,root/'library/movie.iso',root/'library/Conversion files'

    def cross_volume(self,first,second):
        # Support stays on destination; staging is the other volume.
        return Path(first).name!='image.part'

    def test_cross_volume_copy_verified_before_final_rename_and_source_retained(self):
        root,source,output,support=self.fixture();events=[]
        def before():
            self.assertFalse(output.exists())
            self.assertEqual(events[-1],'uhd-copy-verify')
        with patch('dvd2hevc_app.publication.same_volume',side_effect=self.cross_volume):
            result=publish_image(source,output,support,lambda phase,n,total:events.append(phase),before)
        self.assertEqual(result['method'],'verified-copy-and-atomic-rename')
        self.assertEqual(output.read_bytes(),source.read_bytes())
        self.assertFalse(list(support.glob('*.part')))

    def test_source_change_during_transfer_never_publishes_final(self):
        root,source,output,support=self.fixture()
        def progress(phase,n,total):
            if phase=='uhd-copy-output':source.write_bytes(b'changed')
        with patch('dvd2hevc_app.publication.same_volume',side_effect=self.cross_volume):
            with self.assertRaisesRegex(ValueError,'changed during transfer'):publish_image(source,output,support,progress)
        self.assertFalse(output.exists());self.assertTrue(list(support.glob('*.part')))

    def test_changed_destination_copy_is_rejected(self):
        root,source,output,support=self.fixture()
        real_fsync=os.fsync
        def corrupt(fd):
            real_fsync(fd)
            with next(support.glob('*.part')).open('r+b') as stream:stream.write(b'X')
        with patch('dvd2hevc_app.publication.same_volume',side_effect=self.cross_volume),\
             patch('dvd2hevc_app.publication.os.fsync',side_effect=corrupt):
            with self.assertRaisesRegex(ValueError,'verification failed'):publish_image(source,output,support)
        self.assertFalse(output.exists());self.assertTrue(source.exists())

    def test_destination_race_or_failed_final_source_check_preserves_both_inputs(self):
        for race in (False,True):
            root,source,output,support=self.fixture()
            def before():
                if race:output.write_bytes(b'other-owner')
                else:raise ValueError('Source changed before publication')
            with patch('dvd2hevc_app.publication.same_volume',side_effect=self.cross_volume):
                with self.assertRaises((ValueError,FileExistsError)):publish_image(source,output,support,before_publish=before)
            self.assertTrue(source.exists())
            if race:self.assertEqual(output.read_bytes(),b'other-owner')
            else:self.assertFalse(output.exists())

    def test_workspace_preference_cli_selection_and_ram_estimate(self):
        with tempfile.TemporaryDirectory() as temporary,patch.dict(os.environ,{'DVD2HEVC_CONFIG_DIR':temporary},clear=False):
            root=Path(temporary)/'SSD work';save_work_root(root)
            self.assertEqual(configured_work_root(),root.resolve())
            with patch.dict(os.environ,{'DVD2HEVC_WORK_ROOT':''}):self.assertEqual(frontend.managed_work_base(),root.resolve())
            save_work_root('');self.assertIsNone(configured_work_root())
        for command in ('start','auto','queue','watch-folder'):
            args=[command,'source.iso','--work-root','D:/temp']
            if command in ('queue','watch-folder'):args+=['--output-dir','G:/out']
            self.assertEqual(build_parser().parse_args(args).work_root,'D:/temp')
        self.assertEqual(capacity_estimate(4*GIB)['ram_disk_free_ram_bytes'],56*GIB)
        self.assertEqual(capacity_estimate(1)['workspace_bytes'],32*GIB)

    def test_saved_managed_root_still_prunes_after_default_location_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);base=root/'old-ssd';work=frontend.managed_job_work_root('disc-job',base);work.mkdir(parents=True)
            (work/'source.vob').write_bytes(b'temporary');output=root/'verified.iso';output.write_bytes(b'verified')
            job=dict(id='disc-job',status='passed',output=str(output),work_root=str(work),managed_work_root=True,managed_work_base=str(base))
            with patch.object(frontend,'managed_work_base',return_value=root/'new-ssd'):
                self.assertEqual(frontend.prune_completed_work(root/'reports/job.json',job)['files_removed'],1)

    def test_gui_guidance_updates_for_an_iso(self):
        from dvd2hevc_app.gui import DVD2HEVCApp
        with tempfile.TemporaryDirectory() as temporary:
            source=Path(temporary)/'disc.iso';source.write_bytes(b'test')
            app=SimpleNamespace(source_var=SimpleNamespace(get=lambda:str(source)),workspace_note=Mock())
            DVD2HEVCApp._update_workspace_guidance(app)
            self.assertIn('GiB of free RAM',app.workspace_note.configure.call_args.kwargs['text'])

    def test_watch_keeps_its_selected_workspace_and_uhd_player_contract(self):
        from dvd2hevc_app.watched_batches import _watch_namespace
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);incoming=root/'in';incoming.mkdir()
            args=build_parser().parse_args(['watch-folder',str(incoming),'--output-dir',str(root/'out'),
                                           '--work-root',str(root/'ssd')])
            with patch.object(frontend,'WATCH_ROOT',root/'watches'),\
                 patch.object(frontend,'find_stock_vlc_root',return_value=root/'stock') as stock,\
                 patch.object(frontend,'find_patched_vlc_root',side_effect=AssertionError('Wrong player')),\
                 patch.object(frontend,'_spawn_watched_batch',return_value=1):
                _,watch=frontend.create_watched_batch(args,resume_queue=False)
            stock.assert_called_once()
            namespace=_watch_namespace(watch,root/'disc.iso',root/'out/disc.iso',services=frontend)
            self.assertEqual(namespace.work_root,str((root/'ssd').resolve()))
            self.assertEqual(namespace.output_format,'uhd-bd')
