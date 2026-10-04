"""Exercise Tk actions using temporary paths and mocked conversion boundaries."""
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from dvd2hevc_app import gui, frontend, config
from dvd2hevc_app.gui_support import update_row


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        for target, value in (("ensure_active_watchers", None), ("known_job_files", []), ("watched_batch_files", [])):
            patcher = patch.object(gui, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(gui.DVD2HEVCApp, '_refresh_vlc_status')
        patcher.start()
        self.addCleanup(patcher.stop)
        try:
            self.app = gui.DVD2HEVCApp()
        except tk.TclError as exc:
            self.skipTest(f'Tk display unavailable: {exc}')
        self.app.withdraw()
        self.app.update_idletasks()
        self.addCleanup(self.close_app)

    def close_app(self):
        for callback in self.app.tk.call('after', 'info'):
            self.app.after_cancel(callback)
        self.app.destroy()

    def source(self, name='Movie.v1.iso'):
        source = self.root / name
        source.write_bytes(b'fixture')
        return source

    def test_pasted_parent_custom_name_tags_and_blank_default(self):
        a = self.app
        source = self.source()
        a.source_var.set(f' "{source}" ')
        a._update_path_preview()
        self.assertEqual(a.name_var.get(), source.stem)
        self.assertEqual(a._validate_paths()[1], self.root / 'Movie.v1 (DVD) (UHD-BD).iso')
        parent = self.root / 'Converted'
        parent.mkdir()
        a.output_var.set(str(parent))
        self.assertEqual(a._validate_paths()[1], parent / 'Movie.v1 (DVD) (UHD-BD).iso')
        a.output_var.set(str(parent / 'My Cut'))
        self.assertEqual(a._validate_paths()[1], parent / 'My Cut (DVD) (UHD-BD).iso')
        a.filename_tags_var.set(False)
        self.assertEqual(a._validate_paths()[1], parent / 'My Cut.iso')
        a.filename_tags_var.set(True)
        a.output_var.set(str(parent / 'My Cut (dvd) (uhd-bd).ISO'))
        self.assertEqual(a._validate_paths()[1].name, 'My Cut (dvd) (uhd-bd).iso')
        a.output_var.set(str(parent / 'New Library') + '/')
        self.assertEqual(a._validate_paths()[1], parent / 'New Library/Movie.v1 (DVD) (UHD-BD).iso')
        self.assertFalse((parent / 'New Library').exists())

    def test_cleared_and_switched_preset_overrides_do_not_return(self):
        a = self.app
        preset = {**config.resolve_preset('balanced'), 'main_title_quality': 'cq:18',
                  'audio_language_overrides': {'eng': 'compact-stereo'}}
        with patch.object(gui, 'resolve_preset', return_value=preset):
            a._apply_preset('fixture')
        a.override_mode_var.set('None')
        a._language_overrides.clear()
        args = a._settings_namespace(self.source())
        with patch.object(frontend, 'resolve_preset', return_value=preset):
            resolved = frontend.resolve_conversion_settings(args)
        self.assertIsNone(resolved['main_title_quality'])
        self.assertIsNone(resolved['top_n_quality'])
        self.assertEqual(resolved['audio_language_overrides'], {})
        self.assertEqual(a.preset_var.get(), 'Custom')
        a.override_mode_var.set('Top N titles')
        a.top_n_var.set(2)
        args = a._settings_namespace(self.source())
        with patch.object(frontend, 'resolve_preset', return_value=preset):
            resolved = frontend.resolve_conversion_settings(args)
        self.assertIsNone(resolved['main_title_quality'])
        self.assertEqual(resolved['top_n_count'], 2)
        self.assertEqual(resolved['top_n_quality'], 'cq:18')

    def test_queue_settings_and_batch_destinations_are_snapshots(self):
        a = self.app
        sources = [self.source(), self.source('Second.iso')]
        a.source_var.set(str(sources[0]))
        actions = []
        with patch.object(a, '_background', side_effect=lambda label, action, **kw: actions.append(action)):
            a.quality_var.set('cq:22')
            a._queue_one()
        a.quality_var.set('cq:26')
        with patch.object(a, '_prepare_and_queue', return_value='ok') as prepare:
            actions[0]()
            self.assertEqual(prepare.call_args.args[0].quality, 'cq:22')
        a._batch_sources = sources
        a.batch_output_var.set('')
        a._render_batch()
        shown = [a.batch_tree.item(str(source), 'values')[2] for source in sources]
        actions.clear()
        with patch.object(a, '_background', side_effect=lambda label, action, **kw: actions.append(action)):
            a._queue_batch()
        a.filename_tags_var.set(False)
        a._batch_sources = []
        seen = []
        def prepare(args, **kwargs):
            seen.append(args)
            if len(seen) == 1:
                raise RuntimeError('bad disc')
            return self.root / 'job.json', {'id': 'second'}
        with patch.object(a, '_prepare_with_status', side_effect=prepare), patch.object(gui, 'queue_prepared_job', return_value=True), patch.object(gui, 'ensure_dispatcher'):
            result = actions[0]()
        self.assertEqual([args.output for args in seen], shown)
        self.assertTrue(all(args.add_filename_tags for args in seen))
        self.assertIn('Queued 1 of 2', result)
        self.assertIn('bad disc', result)

    def test_scroll_does_not_change_quality_and_inactive_fields(self):
        a = self.app
        a.quality_var.set('cq:22')
        self.assertIn('disabled', a._fields['Target multiplier'].state())
        self.assertIn('disabled', a._fields['Override quality'].state())
        with patch.object(a, '_route_mousewheel', return_value='break'):
            a._fields['Video mode'].event_generate('<MouseWheel>', delta=-120)
        self.assertEqual(a.quality_var.get(), 'cq:22')
        a._language_overrides = {'eng': 'compact-stereo'}
        a._settings_changed()
        self.assertNotIn('disabled', a._fields['Stereo AC-3'].state())

    def test_preset_save_refresh_remove_confirmation_and_missing_error(self):
        a = self.app
        existing = {**config.all_presets(), 'My preset': {'quality': 'cq:20'}}
        with patch.object(gui, 'all_presets', return_value=existing), patch.object(gui.simpledialog, 'askstring', return_value='My preset'), patch.object(gui.messagebox, 'askyesno', return_value=False), patch.object(gui, 'save_named_preset') as save:
            a._save_current_preset()
            save.assert_not_called()
            a._refresh_presets()
            self.assertIn('My preset', a._fields['Preset']['values'])
            self.assertEqual(a.preset_tree.item('My preset', 'values')[0], 'My preset')
            a.preset_tree.selection_set('My preset')
            with patch.object(gui, 'remove_named_preset') as remove:
                a._remove_selected_preset()
                remove.assert_not_called()
        with patch.object(gui, 'resolve_preset', side_effect=ValueError('Missing preset')), patch.object(gui.messagebox, 'showerror') as error:
            a._apply_preset('gone')
            self.assertIn('Missing preset', error.call_args.args[1])

    def test_failure_and_watch_attention_are_visible(self):
        a = self.app
        job = {'id': 'bad', 'status': 'failed', 'error': 'Disk is full', 'source': 'source.iso', 'output': 'out.iso', 'settings': {}}
        a._job_rows['bad'] = (self.root / 'job.json', job)
        update_row(a.jobs_tree, 'bad', ('failed', '0', 'source', '', '', 'out.iso'))
        a.jobs_tree.selection_set('bad')
        with patch.object(gui, 'pipeline_status', return_value={}), patch.object(gui, 'pipeline_percent', return_value=0), patch.object(gui, 'lane_progress_snapshot', return_value={k: (0, 'failed') for k in ('video', 'audio', 'mux')}):
            a._show_job_detail()
        self.assertIn('Disk is full', a.job_detail.get('1.0', 'end'))
        self.assertIn('disabled', a._buttons['_cancel_selected'][0].state())
        self.assertNotIn('disabled', a._buttons['_resume_selected'][0].state())
        watch = {'output_dir': str(self.root), 'ledger': {'source': {'source': 'film.iso', 'status': 'planning-failed', 'error': 'Bad source'}}}
        a._watch_rows['watch'] = (self.root / 'watch.json', watch)
        update_row(a.watch_tree, 'watch', ('active', '', 1, 0, 0, 0, 1))
        a.watch_tree.selection_set('watch')
        a._show_watch_attention()
        self.assertIn('film.iso: planning-failed', a.watch_detail.get('1.0', 'end'))
        self.assertIn('Bad source', a.watch_detail.get('1.0', 'end'))
        a.jobs_tree.delete('bad')
        a._show_job_detail()
        self.assertEqual(a.job_detail.get('1.0', 'end-1c'), '')

    def test_minimum_window_keeps_details_and_watch_reachable(self):
        a = self.app
        a.geometry('940x680')
        a.deiconify()
        a.notebook.select(a.jobs_tab)
        a.update()
        button = a._buttons['_copy_job_details'][0]
        self.assertTrue(button.winfo_ismapped())
        self.assertGreater(a.job_detail.winfo_height(), 40)
        self.assertLess(button.winfo_rooty() - a.winfo_rooty() + button.winfo_height(), a.winfo_height())
        a.notebook.select(a.batch_tab)
        a.update()
        a._tab_canvases[str(a.batch_tab)].yview_moveto(1)
        a.update()
        self.assertLess(a.watch_detail.winfo_rooty() - a.winfo_rooty() + a.watch_detail.winfo_height(), a.winfo_height())


class BackendGuiTests(unittest.TestCase):
    def test_lock_reports_waiting_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            updates = []
            real_open = frontend.os.open
            attempts = []
            def open_lock(*args, **kwargs):
                attempts.append(1)
                if len(attempts) <= 2:
                    raise FileExistsError()
                return real_open(*args, **kwargs)
            with patch.object(frontend, 'JOB_ROOT', root), patch.object(frontend, 'ACTIVE_WORK_LOCK', root / 'lock'), patch.object(frontend.os, 'open', side_effect=open_lock), patch.object(frontend, 'read_json', return_value={'pid': 123}), patch.object(frontend, 'process_alive', return_value=True), patch.object(frontend.time, 'sleep'):
                frontend.acquire_active_work_lock('fixture', progress=updates.append)
            self.assertEqual(len(updates), 1)
            self.assertIn('Waiting for the current disc', updates[0])


if __name__ == '__main__':
    unittest.main()
