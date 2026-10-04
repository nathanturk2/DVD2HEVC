"""Publication, resume and output-contract regressions for the integrated author."""
import argparse
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from dvd2hevc_app import uhd,frontend
from dvd2hevc_app.cli import build_parser
from dvd2hevc_app.contracts import capability_contract,contract_compatible
from dvd2hevc_app.job_progress import _uhd_output_progress
from dvd2hevc_app.pipeline import PipelineError

class UhdWorkflowTests(unittest.TestCase):
    def test_default_and_legacy_contracts_and_queue_options(self):
        parser=build_parser()
        for command in ('auto','start','queue','watch-folder'):
            args=[command,'source.iso']
            if command in ('queue','watch-folder'):args+=['--output-dir','converted']
            settings=frontend.resolve_conversion_settings(parser.parse_args(args))
            self.assertEqual(settings['output_format'],'uhd-bd')
            settings=frontend.resolve_conversion_settings(parser.parse_args(args+['--output-format','dvd-hevc']))
            self.assertEqual(settings['output_format'],'dvd-hevc')
        for mode in ('uhd-bd','dvd-hevc'):
            self.assertTrue(contract_compatible(capability_contract(mode)))
        self.assertEqual(uhd.output_format({}),'dvd-hevc')
        self.assertEqual(frontend.default_output_for(Path('Film (DVD) (UHD-BD).iso'),output_format='dvd-hevc').name,
                         'Film (DVD) (HEVC).iso')

    def test_progress_preserves_completed_uhd_work_and_reserves_success(self):
        events=[dict(scope='uhd-output',task='uhd-author',completed=5,total=10),
                dict(scope='uhd-output',task='uhd-audit',completed=4,total=10)]
        value=_uhd_output_progress(events)
        self.assertEqual(value,91.6)
        events.append(dict(scope='uhd-output',task='uhd-author',completed=0,total=10))
        self.assertEqual(_uhd_output_progress(events),value)
        events.append(dict(scope='uhd-output',task='uhd-complete',completed=1,total=1))
        self.assertLess(_uhd_output_progress(events),100)

    def test_runner_rejects_settings_that_disagree_with_recorded_player_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);path=root/'job.json'
            path.write_text(json.dumps(dict(status='planned',settings={'output_format':'uhd-bd'},
                                            contracts=capability_contract('dvd-hevc'))))
            with patch.object(frontend,'subprocess') as processes:
                with self.assertRaisesRegex(PipelineError,'incompatible.*contract'):
                    frontend._run_job_unlocked(path,quiet=True)
                processes.Popen.assert_not_called()

    def fixtures(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name);source=root/'source.iso';source.write_bytes(b'source')
        binary=root/'tool.exe';binary.write_bytes(b'tool')
        tools=dict(stock_vlc=str(root),bdj_api=str(binary),java_home=str(root),
                   tsmuxer=str(binary),udf_tool=str(binary))
        def author(source,folder,**kwargs):
            folder.mkdir();(folder/'conversion-report.json').write_text(json.dumps(
                dict(complete=True,schema='dvd2uhd-author-v1',source=str(source))))
        def create(folder,image,**kwargs):
            image.write_bytes(b'verified-image')
            return dict(image=str(image),verified=True,files=[])
        for target,value in [('require_uhd_tools',tools),('check_structure',{}),('_emit',None)]:
            context=patch.object(uhd,target,return_value=value);context.start();self.addCleanup(context.stop)
        for target,options in [('dvd2uhd.author.author',dict(side_effect=author)),
                               ('dvd2uhd.audit.audit',dict(return_value={'missing':[]})),
                               ('dvd2uhd.iso.create',dict(side_effect=create)),
                               ('dvd2uhd.iso.payload',dict(return_value=[])),
                               ('dvd2hevc_app.tools.discover_tools',dict(return_value={'ffmpeg':str(binary),'ffprobe':str(binary)}))]:
            context=patch(target,**options);context.start();self.addCleanup(context.stop)
        context=patch.dict(os.environ,os.environ.copy());context.start();self.addCleanup(context.stop)
        return root,source,root/'output.iso',root/'work'

    def test_unrelated_completed_disc_in_working_directory_is_not_reused(self):
        root,source,output,work=self.fixtures()
        foreign=root/'foreign';foreign.mkdir()
        (foreign/'conversion-report.json').write_text('{"complete":true}')
        previous_cwd=Path.cwd()
        try:
            os.chdir(foreign)
            with patch.object(uhd,'stock_vlc_gate',return_value={'passed':True}):
                result=uhd.author_uhd(source,output,work)
            self.assertEqual(Path(result['folder']).parent,work)
            self.assertNotEqual(Path(result['folder']),foreign)
        finally:
            os.chdir(previous_cwd)

    def test_new_output_directory_is_created(self):
        root,source,output,work=self.fixtures()
        output=root/'new-output'/'nested'/'output.iso'
        with patch.object(uhd,'stock_vlc_gate',return_value={'passed':True}):
            result=uhd.author_uhd(source,output,work)
        self.assertTrue(result['passed']);self.assertTrue(output.is_file())

    def test_source_changed_during_authoring_does_not_publish(self):
        root,source,output,work=self.fixtures()
        def gate(*args):
            source.write_bytes(b'changed-during-authoring')
            return {'passed':True}
        with patch.object(uhd,'stock_vlc_gate',side_effect=gate):
            with self.assertRaisesRegex(PipelineError,'Source changed during'):
                uhd.author_uhd(source,output,work)
        self.assertFalse(output.exists())
        self.assertEqual(len(list(root.glob('.output.iso.*.part'))),1)
        self.assertFalse(json.loads((work/'uhd-output.json').read_text())['passed'])

    def test_cached_authoring_report_must_belong_to_requested_source(self):
        root,source,output,work=self.fixtures()
        with patch.object(uhd,'stock_vlc_gate',side_effect=PipelineError('gate failed')):
            with self.assertRaises(PipelineError):uhd.author_uhd(source,output,work)
        previous=json.loads((work/'uhd-output.json').read_text())
        report=Path(previous['folder'])/'conversion-report.json'
        data=json.loads(report.read_text());data['source']=str(root/'foreign.iso')
        report.write_text(json.dumps(data))
        with patch.object(uhd,'stock_vlc_gate',return_value={'passed':True}):
            result=uhd.author_uhd(source,output,work)
        self.assertNotEqual(result['folder'],previous['folder'])

    def test_failed_stock_vlc_gate_leaves_final_output_absent(self):
        root,source,output,work=self.fixtures()
        with patch.object(uhd,'stock_vlc_gate',side_effect=PipelineError('gate failed')):
            with self.assertRaisesRegex(PipelineError,'gate failed'):
                uhd.author_uhd(source,output,work)
        self.assertFalse(output.exists())
        self.assertEqual(len(list(root.glob('.output.iso.*.part'))),1)
        self.assertFalse(json.loads((work/'uhd-output.json').read_text())['passed'])

    def test_success_resume_verifies_payload_and_rejects_changed_source(self):
        root,source,output,work=self.fixtures()
        with patch.object(uhd,'stock_vlc_gate',return_value={'passed':True}) as gate:
            result=uhd.author_uhd(source,output,work)
            self.assertTrue(result['passed']);self.assertEqual(output.read_bytes(),b'verified-image')
            self.assertEqual(json.loads(output.with_suffix('.manifest.json').read_text())['image'],str(output))
            uhd.author_uhd(source,output,work)
            self.assertEqual(gate.call_count,1)
            source.write_bytes(b'changed-source')
            with self.assertRaisesRegex(PipelineError,'not.*same verified'):
                uhd.author_uhd(source,output,work)
            self.assertEqual(output.read_bytes(),b'verified-image')

    def test_output_created_during_gate_is_never_overwritten(self):
        root,source,output,work=self.fixtures()
        def gate(*args):output.write_bytes(b'another-writer');return {'passed':True}
        with patch.object(uhd,'stock_vlc_gate',side_effect=gate):
            with self.assertRaisesRegex(PipelineError,'Output appeared'):
                uhd.author_uhd(source,output,work)
        self.assertEqual(output.read_bytes(),b'another-writer')

    def test_resume_recovers_interruption_between_image_and_manifest_publication(self):
        root,source,output,work=self.fixtures()
        real_write=uhd.write_json_atomic
        def interrupted_write(path,value):
            if Path(path)==output.with_suffix('.manifest.json'):
                raise OSError('interrupted manifest publication')
            return real_write(path,value)
        with patch.object(uhd,'stock_vlc_gate',return_value={'passed':True}) as gate:
            with patch.object(uhd,'write_json_atomic',side_effect=interrupted_write):
                with self.assertRaisesRegex(OSError,'interrupted'):
                    uhd.author_uhd(source,output,work/'uhd')
            self.assertTrue(output.is_file())
            self.assertFalse(output.with_suffix('.manifest.json').exists())
            self.assertTrue(frontend._existing_output_is_authored_job_artifact(
                dict(output=str(output),work_root=str(work),settings={'output_format':'uhd-bd'})))
            result=uhd.author_uhd(source,output,work/'uhd')
            self.assertTrue(result['passed'])
            self.assertTrue(output.with_suffix('.manifest.json').is_file())
            self.assertEqual(gate.call_count,1)

if __name__=='__main__':unittest.main()
