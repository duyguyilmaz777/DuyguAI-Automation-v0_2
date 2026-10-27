from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from models.job import JobOrder, JobStatus, SceneItem
from providers.base import (
    BrowserCrashedError,
    CDPDisconnectError,
    GemAccessDeniedError,
    RateLimitError,
    SessionExpiredError,
    TemporaryServerError,
)
from services.error_classifier import ErrorCategory, classify_error
from services.job_queue import JobQueue
from services.profile_manager import ChromeProfile, GemAccessInfo, ProfileManager
from services.queue_runner import QueueRunner
from tests.fakes import FakeProvider


class QueueAndRecoveryTests(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp_dir.name)

        self.db_path = self.tmp_path / "test_queue.db"
        self.profiles_state_path = self.tmp_path / "profiles_state.json"
        self.outputs_dir = self.tmp_path / "outputs"
        self.jobs_dir = self.tmp_path / "jobs"

        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

        # Patch workflow dirs to temporary directory
        import services.workflow as wf
        self._old_outputs = wf.OUTPUTS_DIR
        self._old_jobs = wf.JOBS_DIR
        wf.OUTPUTS_DIR = self.outputs_dir
        wf.JOBS_DIR = self.jobs_dir

        self.queue = JobQueue(db_path=self.db_path)
        self.profile_mgr = ProfileManager(state_file=self.profiles_state_path)
        self.config = {
            "dry_run": False,
            "target_folder_id": "test-folder-id",
            "video_timeout_minutes": 1,
            "quota_cooldown_hours": 1.0,
            "retry_backoff_base_seconds": 2,
            "max_retries_per_job": 3,
            "stale_lock_timeout_seconds": 10,
        }
        self.runner = QueueRunner(
            job_queue=self.queue,
            profile_manager=self.profile_mgr,
            config=self.config,
            worker_id="test_worker_1",
        )

    def tearDown(self):
        import services.workflow as wf
        wf.OUTPUTS_DIR = self._old_outputs
        wf.JOBS_DIR = self._old_jobs
        try:
            self.runner.stop(timeout=1.0)
        except Exception:
            pass
        try:
            self._tmp_dir.cleanup()
        except Exception:
            pass

    def _create_sample_job(self, job_id: str = "job_001", brand: str = "TestBrand", scene_count: int = 1) -> JobOrder:
        effective_brand = f"{brand}_{job_id}"
        scenes = [
            SceneItem(index=i + 1, title=f"Sahne {i + 1}", description=f"Açıklama {i + 1}")
            for i in range(scene_count)
        ]
        return JobOrder(
            job_id=job_id,
            brief="Örnek brief açıklaması",
            scenario="Sahne 1: Ürün döner",
            content_type="ürün reklamı",
            video_format="9:16",
            scene_count=scene_count,
            seconds_per_scene=5,
            voiceover=False,
            music=False,
            editing_notes="",
            extra_notes="",
            target_folder_id="test-folder-id",
            gem_url="https://gemini.google.com/gem/af11977bbdce",
            reference_images=[],
            brand_name=effective_brand,
            dry_run=False,
            scenes=scenes,
            output_dir=str(self.outputs_dir / effective_brand),
        )

    # -------------------------------------------------------------------------
    # Senaryo 1: Başarılı Üretim
    # -------------------------------------------------------------------------
    def test_01_successful_production_marks_job_completed(self):
        job = self._create_sample_job("job_success")
        self.queue.enqueue_job(job)

        provider = FakeProvider(upload_verified=True)
        processed = self.runner.step(custom_provider=provider)

        self.assertTrue(processed)
        updated_job = self.queue.get_job("job_success")
        self.assertIsNotNone(updated_job)
        self.assertEqual(updated_job.status, "completed")
        self.assertFalse(updated_job.is_locked)
        self.assertFalse(updated_job.locked_by)
        completed_scenes = [s for s in updated_job.scenes if s.status == "video_done"]
        self.assertEqual(len(completed_scenes), 1)

    # -------------------------------------------------------------------------
    # Senaryo 2: Kota Hatası (Quota / Rate Limit)
    # -------------------------------------------------------------------------
    def test_02_quota_error_sets_waiting_and_next_retry_at(self):
        job = self._create_sample_job("job_quota")
        self.queue.enqueue_job(job)

        provider = FakeProvider(video_error=RateLimitError("Gemini video kullanım sınırına ulaştınız."))
        processed = self.runner.step(custom_provider=provider)

        self.assertTrue(processed)
        updated = self.queue.get_job("job_quota")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.status, "waiting")
        self.assertEqual(updated.last_error_category, "quota_exceeded")
        self.assertIsNotNone(updated.next_retry_at)
        # 1 saat sonrasına planlanmış olmalı
        retry_dt = datetime.fromisoformat(updated.next_retry_at)
        self.assertGreater(retry_dt, datetime.now(timezone.utc))
        self.assertFalse(updated.is_locked)

    # -------------------------------------------------------------------------
    # Senaryo 3: Geçici Bağlantı Hatası (Temporary Server / Network Error)
    # -------------------------------------------------------------------------
    def test_03_temporary_connection_error_retries_safely(self):
        job = self._create_sample_job("job_temp_err")
        self.queue.enqueue_job(job)

        provider = FakeProvider(connect_error=TemporaryServerError("Sunucu 503 geçici yanıt veremiyor."))
        processed = self.runner.step(custom_provider=provider)

        self.assertTrue(processed)
        updated = self.queue.get_job("job_temp_err")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.status, "waiting")
        self.assertEqual(updated.last_error_category, "temporary_server_error")
        self.assertEqual(updated.retry_count, 0)
        self.assertIsNotNone(updated.next_retry_at)
        self.assertFalse(updated.is_locked)

    # -------------------------------------------------------------------------
    # Senaryo 4: CDP Bağlantısının Kesilmesi (CDP Disconnect)
    # -------------------------------------------------------------------------
    def test_04_cdp_disconnection_handled_cleanly(self):
        job = self._create_sample_job("job_cdp_disc")
        self.queue.enqueue_job(job)

        provider = FakeProvider(connect_error=CDPDisconnectError("Target page crashed or CDP websocket closed."))
        processed = self.runner.step(custom_provider=provider)

        self.assertTrue(processed)
        updated = self.queue.get_job("job_cdp_disc")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.status, "waiting")
        self.assertEqual(updated.last_error_category, "network_cdp_disconnected")
        self.assertFalse(updated.is_locked)

    # -------------------------------------------------------------------------
    # Senaryo 5: Uygulamanın Kapanıp Yeniden Açılması (Startup Crash Recovery)
    # -------------------------------------------------------------------------
    def test_05_application_restart_recovers_interrupted_jobs(self):
        job = self._create_sample_job("job_crashed")
        job.is_locked = True
        job.locked_by = "old_dead_worker"
        job.locked_at = datetime.now(timezone.utc).isoformat()
        job.status = "running"
        self.queue.enqueue_job(job)

        # Doğrudan DB'de running ve locked yap (simüle edilen elektrik kesintisi / çökme)
        with self.queue._get_connection() as conn:
            conn.execute("UPDATE jobs SET status = 'running', is_locked = 1, locked_by = 'dead_proc' WHERE job_id = 'job_crashed'")

        # Yeni bir kuyruk oturumu başlat (uygulama açılışı)
        new_queue = JobQueue(db_path=self.db_path)
        recovered_count = new_queue.recover_interrupted_jobs()

        self.assertEqual(recovered_count, 1)
        recovered_job = new_queue.get_job("job_crashed")
        self.assertEqual(recovered_job.status, "pending")
        self.assertFalse(recovered_job.is_locked)
        self.assertFalse(recovered_job.locked_by)

    # -------------------------------------------------------------------------
    # Senaryo 6: Tamamlanmış İşin İkinci Kez Çalıştırılmaması
    # -------------------------------------------------------------------------
    def test_06_completed_job_is_not_rerun(self):
        job = self._create_sample_job("job_done")
        self.queue.enqueue_job(job)
        self.queue.mark_completed("job_done", ["video1.mp4"])

        # Kuyruktan iş alma girişimi
        acquired = self.queue.acquire_next_job("worker_2")
        self.assertIsNone(acquired, "Tamamlanmış iş kuyruktan yeni iş olarak alınmamalıdır.")

    # -------------------------------------------------------------------------
    # Senaryo 7: Aynı İşin İki Kez Eşzamanlı Başlatılmasının Engellenmesi
    # -------------------------------------------------------------------------
    def test_07_concurrent_execution_prevented_by_lock(self):
        job = self._create_sample_job("job_concurrent")
        self.queue.enqueue_job(job)

        worker1_job = self.queue.acquire_next_job("worker_alpha")
        self.assertIsNotNone(worker1_job)
        self.assertEqual(worker1_job.job_id, "job_concurrent")
        self.assertTrue(worker1_job.is_locked)

        # Aynı anda 2. bir worker işi almaya çalışır
        worker2_job = self.queue.acquire_next_job("worker_beta")
        self.assertIsNone(worker2_job, "Aynı iş ikinci bir worker tarafından eşzamanlı kilitlenemez.")

    # -------------------------------------------------------------------------
    # Senaryo 8: Gem Erişiminin Bulunmaması (Gem Access Denied)
    # -------------------------------------------------------------------------
    def test_08_missing_gem_access_flags_needs_attention(self):
        job = self._create_sample_job("job_gem_denied")
        self.queue.enqueue_job(job)

        # Profile manager üzerinde bu Gem'i denied yap
        active_prof = self.profile_mgr.get_active_profile()
        gem_id = "af11977bbdce"
        if gem_id in active_prof.gem_access:
            active_prof.gem_access[gem_id].status = "denied"
            active_prof.gem_access[gem_id].notes = "Erişim izni bulunmuyor."
            self.profile_mgr._save_state()

        processed = self.runner.step()
        self.assertTrue(processed)

        updated = self.queue.get_job("job_gem_denied")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.status, "needs_attention")
        self.assertEqual(updated.last_error_category, "gem_access_denied")
        self.assertFalse(updated.is_locked)

    # -------------------------------------------------------------------------
    # Senaryo 9: Bekleme Süresi Dolmadan İşin Başlatılmaması
    # -------------------------------------------------------------------------
    def test_09_waiting_job_not_started_before_next_retry_at(self):
        job = self._create_sample_job("job_future_retry")
        self.queue.enqueue_job(job)

        future_time = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        self.queue.mark_waiting("job_future_retry", future_time)

        # Süresi dolmadığı için alınamamalı
        acquired = self.queue.acquire_next_job("worker_1")
        self.assertIsNone(acquired)

    # -------------------------------------------------------------------------
    # Senaryo 10: Bekleme Süresi Dolduğunda Kuyruğun Devam Etmesi
    # -------------------------------------------------------------------------
    def test_10_waiting_job_resumes_when_next_retry_at_passed(self):
        job = self._create_sample_job("job_past_retry")
        self.queue.enqueue_job(job)

        past_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        self.queue.mark_waiting("job_past_retry", past_time)

        # Süresi geçmiş olduğu için alınabilmeli
        acquired = self.queue.acquire_next_job("worker_1")
        self.assertIsNotNone(acquired)
        self.assertEqual(acquired.job_id, "job_past_retry")
        self.assertEqual(acquired.status, "running")

    # -------------------------------------------------------------------------
    # Senaryo 11: Çıktı Dosyası Mevcut Ancak İş Durumu Güncellenmemiş Kurtarma
    # -------------------------------------------------------------------------
    def test_11_output_recovery_when_video_exists_on_disk_but_state_unrecorded(self):
        job = self._create_sample_job("job_disk_recovery", brand="BrandRecovered", scene_count=1)
        self.queue.enqueue_job(job)

        # Diskte çıktıları hazırla (örneğin iş bitti ama elektrik anında kesildi)
        brand_dir = Path(job.output_dir)
        vids_dir = brand_dir / "03_Videolar"
        vids_dir.mkdir(parents=True, exist_ok=True)
        full_vid = vids_dir / "tam_video.mp4"
        full_vid.write_bytes(b"x" * 200000)  # > 100KB geçerli video
        scene1_vid = vids_dir / "sahne_1.mp4"
        scene1_vid.write_bytes(b"x" * 100000)
        (brand_dir / ".delivered").write_text("{\"delivered\": true}")

        provider = FakeProvider()
        processed = self.runner.step(custom_provider=provider)

        self.assertTrue(processed)
        # FakeProvider çağrılmamalı (send_prompt veya wait_for_new_video çağrılmamalı)
        self.assertNotIn("send_prompt", provider.calls)

        updated = self.queue.get_job("job_disk_recovery")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.status, "completed")

    # -------------------------------------------------------------------------
    # Senaryo 12: Bozuk veya Süresi Dolmuş Oturum (Session Expired)
    # -------------------------------------------------------------------------
    def test_12_expired_session_detected_and_marked_needs_attention(self):
        job = self._create_sample_job("job_session_expired")
        self.queue.enqueue_job(job)

        provider = FakeProvider(connect_error=SessionExpiredError("Google oturumu sona ermiş; yeniden giriş yapılması gerekiyor."))
        processed = self.runner.step(custom_provider=provider)

        self.assertTrue(processed)
        updated = self.queue.get_job("job_session_expired")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.status, "needs_attention")
        self.assertEqual(updated.last_error_category, "session_expired")
        self.assertFalse(updated.is_locked)
        # Sonsuz tekrar denenmemeli, kullanıcı müdahalesi beklenmeli
        self.assertIn("oturum", updated.last_error.lower())


if __name__ == "__main__":
    unittest.main()
