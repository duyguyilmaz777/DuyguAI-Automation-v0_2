import json
import tempfile
import unittest
from pathlib import Path


class StartupTests(unittest.TestCase):
    def test_mac_ver_is_populated(self):
        import platform

        version = platform.mac_ver()[0]
        self.assertTrue(version, "platform.mac_ver() boş dönmemeli")
        self.assertTrue(version[0].isdigit(), version)

    def test_pyexpat_loads(self):
        import pyexpat

        self.assertTrue(hasattr(pyexpat, "ParserCreate"))

    def test_app_module_imports(self):
        import app

        self.assertTrue(hasattr(app, "App"))
        self.assertTrue(hasattr(app, "load_config"))

    def test_config_keeps_dry_run_true(self):
        import app

        cfg = json.loads(Path(app.CONFIG_PATH).read_text(encoding="utf-8"))
        self.assertIn("dry_run", cfg)
        self.assertIsInstance(cfg["dry_run"], bool)


class JobPersistenceTests(unittest.TestCase):
    def test_workflow_writes_job_file(self):
        import services.workflow as wf
        from tests.fakes import FakeProvider
        from tests.test_workflow import CFG, collect

        lines, log = collect()
        with tempfile.TemporaryDirectory() as tmp:
            old_jobs = wf.JOBS_DIR
            old_outputs = wf.OUTPUTS_DIR
            wf.JOBS_DIR = Path(tmp) / "jobs"
            wf.OUTPUTS_DIR = Path(tmp) / "outputs"
            wf.JOBS_DIR.mkdir(parents=True, exist_ok=True)
            wf.OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
            try:
                report = wf.run_content_workflow(dict(CFG), [], log, provider=FakeProvider())
                path = wf.JOBS_DIR / f"{report.job.job_id}.json"
                self.assertTrue(path.is_file(), path)
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(data["job"]["job_id"], report.job.job_id)
                self.assertTrue(data["job"]["dry_run"])
                self.assertEqual(data["overall_status"], "dry_run")
            finally:
                wf.JOBS_DIR = old_jobs
                wf.OUTPUTS_DIR = old_outputs


class AppStateTests(unittest.TestCase):
    def setUp(self):
        import app
        if app.PENDING_JOB_FILE.is_file():
            try:
                app.PENDING_JOB_FILE.unlink()
            except Exception:
                pass
        self.app = app.App()
        self.app.switch_project(app.PROJECT_DUYGU)
        self.app.withdraw()

    def tearDown(self):
        import app
        if app.PENDING_JOB_FILE.is_file():
            try:
                app.PENDING_JOB_FILE.unlink()
            except Exception:
                pass
        try:
            self.app.switch_project(app.PROJECT_DUYGU)
        except Exception:
            pass
        self.app.destroy()

    def test_reset_project_fields_clears_form(self):
        self.app.brief_text.insert("1.0", "Örnek brief")
        self.app.drive_var.set("folder-123")
        self.app.scene_count.delete(0, "end")
        self.app.scene_count.insert(0, "5")

        self.app.reset_project_fields(silent=True)

        self.assertEqual(self.app.brief_text.get("1.0", "end").strip(), "")
        self.assertEqual(self.app.drive_var.get().strip(), "")
        self.assertEqual(self.app.scene_count.get(), "1")

    def test_pending_job_lifecycle_resets_on_success(self):
        import app
        from unittest.mock import MagicMock

        # 1. Draft save
        self.app.brief_text.delete("1.0", "end")
        self.app.brief_text.insert("1.0", "Yeni ürün briefi")
        self.app.drive_var.set("drive-folder-abc")
        self.app._save_pending_job()
        self.assertTrue(app.PENDING_JOB_FILE.is_file())

        # 2. Simulated failure preserves pending job
        failed_report = MagicMock()
        failed_report.overall_status = "failed"
        failed_report.delivered = False
        self.app._on_run_finished(failed_report)
        self.assertTrue(app.PENDING_JOB_FILE.is_file())
        self.assertEqual(self.app.brief_text.get("1.0", "end").strip(), "Yeni ürün briefi")

        # 3. Simulated success clears pending job and form
        from unittest.mock import patch
        success_report = MagicMock()
        success_report.overall_status = "success"
        success_report.delivered = True
        with patch("tkinter.messagebox.showinfo"):
            self.app._on_run_finished(success_report)
        self.assertFalse(app.PENDING_JOB_FILE.is_file())
        self.assertEqual(self.app.brief_text.get("1.0", "end").strip(), "")
        self.assertEqual(self.app.drive_var.get().strip(), "")

    def test_switch_project_updates_active_state_and_gem(self):
        from models.job import PROJECT_DUYGU, PROJECT_IKIZLER

        # Default project Duygu AI Studio
        self.assertEqual(self.app.current_project, PROJECT_DUYGU)
        self.assertEqual(self.app.cfg["gem_url"], "https://gemini.google.com/gem/af11977bbdce")

        # Switch to İkizler Masal Durağı
        self.app.switch_project(PROJECT_IKIZLER)
        self.assertEqual(self.app.current_project, PROJECT_IKIZLER)
        self.assertEqual(self.app.cfg["gem_url"], "https://gemini.google.com/gem/8e63671e6329")
        self.assertEqual(self.app.ikizler_form_frame.winfo_manager(), "pack")
        self.assertEqual(self.app.duygu_form_frame.winfo_manager(), "")

        # Switch back to Duygu AI Studio
        self.app.switch_project(PROJECT_DUYGU)
        self.assertEqual(self.app.current_project, PROJECT_DUYGU)
        self.assertEqual(self.app.cfg["gem_url"], "https://gemini.google.com/gem/af11977bbdce")
        self.assertEqual(self.app.duygu_form_frame.winfo_manager(), "pack")
        self.assertEqual(self.app.ikizler_form_frame.winfo_manager(), "")

    def test_ikizler_masal_duragi_fields_and_reset(self):
        from models.job import PROJECT_IKIZLER

        self.app.switch_project(PROJECT_IKIZLER)
        self.app.masal_topic_entry.delete(0, "end")
        self.app.masal_topic_entry.insert(0, "Kayıp Yavru Kedi")
        self.app.masal_age_combo.set("5-7 Yaş")
        self.app.masal_chars_entry.delete(0, "end")
        self.app.masal_chars_entry.insert(0, "Mırmır ve Ali")
        self.app.masal_goal_entry.delete(0, "end")
        self.app.masal_goal_entry.insert(0, "Sorumluluk Bilinci")
        self.app.masal_story_text.delete("1.0", "end")
        self.app.masal_story_text.insert("1.0", "1. Sahne: Parkta kedi bulunur.")

        cfg = self.app._get_current_job_cfg()
        self.assertEqual(cfg["project_name"], PROJECT_IKIZLER)
        self.assertEqual(cfg["gem_url"], "https://gemini.google.com/gem/8e63671e6329")
        self.assertIn("Masal Konusu: Kayıp Yavru Kedi", cfg["brief"])
        self.assertIn("Hedef Yaş Grubu: 5-7 Yaş", cfg["brief"])
        self.assertIn("Mırmır ve Ali", cfg["brief"])
        self.assertIn("Sorumluluk Bilinci", cfg["brief"])
        self.assertEqual(cfg["scenario"], "1. Sahne: Parkta kedi bulunur.")
        self.assertEqual(cfg["content_type"], "çocuk masalı")

        # Test reset
        self.app.reset_project_fields(silent=True)
        self.assertEqual(self.app.masal_topic_entry.get().strip(), "")
        self.assertEqual(self.app.masal_chars_entry.get().strip(), "")
        self.assertEqual(self.app.masal_story_text.get("1.0", "end").strip(), "")

    def test_prompt_project_selection_modal_creation(self):
        import tkinter as tk

        # Force open the modal dialog
        self.app.prompt_project_selection(force=True)

        # Locate the Toplevel window
        toplevels = [w for w in self.app.winfo_children() if isinstance(w, tk.Toplevel)]
        self.assertTrue(len(toplevels) >= 1)
        dialog = toplevels[0]

        # Check title and destroy
        self.assertIn("Proje Seçimi", dialog.title())
        dialog.destroy()

    def test_start_run_ikizler_initiates_workflow(self):
        from unittest.mock import patch, MagicMock
        from models.job import PROJECT_IKIZLER

        self.app.switch_project(PROJECT_IKIZLER)
        self.app.masal_topic_entry.delete(0, "end")
        self.app.masal_topic_entry.insert(0, "Cesur Sincap")
        self.app.masal_story_text.delete("1.0", "end")
        self.app.masal_story_text.insert("1.0", "1. Sahne: Ormanda macera.")
        self.app.dry_run_var.set(True)

        with patch("app.Automation") as mock_auto, \
             patch("tkinter.messagebox.askyesno", return_value=True), \
             patch("tkinter.messagebox.showerror"), \
             patch("tkinter.messagebox.showwarning"), \
             patch.object(self.app, "_save_pending_job"):
            mock_inst = MagicMock()
            mock_auto.return_value = mock_inst
            mock_inst.run.return_value = MagicMock(overall_status="dry_run", delivered=False)

            self.app.start_run()
            self.assertTrue(self.app._running)
            self.assertIsNotNone(self.app.worker)
            self.app.worker.join(timeout=2.0)
            mock_inst.run.assert_called_once()


