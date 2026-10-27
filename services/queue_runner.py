from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from models.job import JobOrder, JobStatus, StageStatus, WorkflowReport
from providers.base import RateLimitError
from services.error_classifier import ErrorCategory, ErrorInfo, classify_error
from services.job_queue import JobQueue
from services.profile_manager import (
    KNOWN_GEMS,
    ProfileManager,
    extract_gem_id_from_url,
)
from services.workflow import run_content_workflow

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = APP_DIR / "config.json"


def load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


class QueueRunner:
    """Kalıcı SQLite iş kuyruğunu güvenli ve otonom şekilde yürüten servis motoru.
    
    Özellikler:
    - Atomik kilitler ile aynı işin çift çalıştırılmasını engeller.
    - Elektrik kesintisi veya kapanma sonrası yarıda kalan işleri kurtarır (startup recovery).
    - Gem erişimini işe başlamadan önce doğrular; yetkisiz ise 'needs_attention' olarak işaretler.
    - Disk üzerindeki mevcut tamamlanmış çıktıları kontrol eder; gereksiz yere baştan üretmez.
    - Hata sınıflandırması ile kota, geçici hata, ağ kesintisi ve politika retlerini ayrı yönetir.
    - Kota bekleme sürelerini config.json üzerinden yönetir, CPU'yu boşa döndürmez (event-driven).
    - Duraklatma, devam ettirme ve güvenli durdurma destekler.
    """

    def __init__(
        self,
        job_queue: JobQueue | None = None,
        profile_manager: ProfileManager | None = None,
        log_callback: Callable[[str], None] | None = None,
        config: dict[str, Any] | None = None,
        worker_id: str | None = None,
    ):
        self.config = config or load_config()
        self.job_queue = job_queue or JobQueue()
        self.profile_manager = profile_manager or ProfileManager()
        self.log_callback = log_callback or (lambda msg: None)
        self.worker_id = worker_id or f"worker_{uuid.uuid4().hex[:8]}"

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._is_running = False
        self._current_job: JobOrder | None = None
        self._lock = threading.Lock()

        # Başlangıç kurtarmasını otomatik tetikle
        recovered = self.job_queue.recover_interrupted_jobs()
        if recovered > 0:
            self._log(f"🔄 Sistem kurtarma: Yarıda kalmış veya kilitli {recovered} iş kuyruğa geri alındı.")

    def _log(self, message: str) -> None:
        try:
            self.log_callback(message)
        except Exception:
            pass
        logger.info(message)

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def current_job(self) -> JobOrder | None:
        return self._current_job

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            cur_job_data = None
            if self._current_job:
                cur_job_data = {
                    "job_id": self._current_job.job_id,
                    "brand_name": self._current_job.brand_name,
                    "content_type": self._current_job.content_type,
                    "gem_url": self._current_job.gem_url,
                    "status": self._current_job.status,
                }

            summary = self.job_queue.get_queue_summary()
            return {
                "worker_id": self.worker_id,
                "is_running": self._is_running,
                "is_paused": summary.get("is_paused", False),
                "current_job": cur_job_data,
                "counts": summary.get("counts", {}),
                "earliest_retry_at": summary.get("earliest_retry_at"),
            }

    # -------------------------------------------------------------------------
    # İşçi Yaşam Döngüsü Kontrolleri
    # -------------------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._is_running:
                return
            self._stop_event.clear()
            self._wake_event.clear()
            self._is_running = True
            self._thread = threading.Thread(target=self._worker_loop, daemon=True, name="QueueRunnerWorker")
            self._thread.start()
            self._log(f"🟢 Kuyruk motoru başlatıldı (Worker: {self.worker_id})")

    def stop(self, timeout: float = 5.0) -> None:
        try:
            from providers.gemini import GeminiWebProvider
            GeminiWebProvider.abort_all()
        except Exception:
            pass

        with self._lock:
            if not self._is_running:
                self._log("⚪ Kuyruk motoru zaten çalışmıyor.")
                return
            self._log("⏹️ Kuyruk motoru durduruluyor…")
            self._stop_event.set()
            self._wake_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

        with self._lock:
            self._is_running = False
            # Eğer aktif iş kilitliyse kilidi bırak
            if self._current_job:
                try:
                    self.job_queue.release_job_lock(self._current_job.job_id, self.worker_id)
                except Exception:
                    pass
                self._current_job = None
            self._log("⚪ Kuyruk motoru durduruldu.")

    def pause(self) -> None:
        self.job_queue.pause_queue()
        self._wake_event.set()
        self._log("⏸️ Kuyruk duraklatıldı. Mevcut iş bitiminde yeni iş alınmayacak.")

    def resume(self) -> None:
        self.job_queue.resume_queue()
        self._wake_event.set()
        self._log("▶️ Kuyruk devam ettirildi.")

    def wake_up(self) -> None:
        """Yeni bir iş eklendiğinde bekleyen döngüyü hemen uyandırır."""
        self._wake_event.set()

    # -------------------------------------------------------------------------
    # Ana Çalışma Döngüsü
    # -------------------------------------------------------------------------
    def _worker_loop(self) -> None:
        poll_interval = float(self.config.get("queue_poll_interval_seconds", 5))

        while not self._stop_event.is_set():
            try:
                processed = self.step()
                if not processed:
                    # Uygun iş yok veya kuyruk beklemede
                    wait_time = self._calculate_sleep_time(default_poll=poll_interval)
                    self._wake_event.wait(timeout=wait_time)
                    self._wake_event.clear()
            except Exception as exc:
                self._log(f"Kuyruk döngüsü beklenmeyen hata: {exc}")
                time.sleep(2)

    def _calculate_sleep_time(self, default_poll: float = 5.0) -> float:
        """CPU'yu boşa çalıştırmamak için bir sonraki deneme anına kadar hesaplanan süre."""
        summary = self.job_queue.get_queue_summary()
        if summary.get("is_paused"):
            return 10.0

        earliest = summary.get("earliest_retry_at")
        if earliest:
            try:
                earliest_dt = datetime.fromisoformat(earliest)
                delta = (earliest_dt - datetime.now(timezone.utc)).total_seconds()
                if delta > 0:
                    return min(delta, 300.0)  # En fazla 5 dakikada bir liveness kontrolü
                else:
                    return 0.5  # Vakti gelmiş, hemen çalış
            except Exception:
                pass
        return default_poll

    # -------------------------------------------------------------------------
    # Tek Adım Yürütme (step - Test ve Manuel Çağrılar için)
    # -------------------------------------------------------------------------
    def step(self, custom_provider: Any = None) -> bool:
        """Kuyruktan bir sonraki işi alır ve yürütür. İş işlendiyse True döner."""
        if self._stop_event.is_set():
            return False

        if self.job_queue.is_queue_paused():
            return False

        stale_sec = int(self.config.get("stale_lock_timeout_seconds", 900))
        job = self.job_queue.acquire_next_job(worker_id=self.worker_id, stale_lock_seconds=stale_sec)
        if not job:
            return False

        with self._lock:
            self._current_job = job

        try:
            self._execute_job(job, custom_provider=custom_provider)
            return True
        finally:
            with self._lock:
                self._current_job = None

    # -------------------------------------------------------------------------
    # İş Yürütme & Güvenlik Doğrulamaları
    # -------------------------------------------------------------------------
    def _execute_job(self, job: JobOrder, custom_provider: Any = None) -> None:
        self._log(f"🚀 [İş #{job.job_id}] Alındı | Marka: {job.brand_name} | Tür: {job.content_type}")

        # 1. Çıktı klasörü ve disk kontrolü: İş zaten eksiksiz üretilmiş mi?
        if self._check_already_completed_on_disk(job):
            self._log(f"✅ [İş #{job.job_id}] Tüm video çıktıları diskte eksiksiz mevcut; yeniden üretim atlandı ve tamamlandı.")
            self.job_queue.mark_completed(job.job_id, [s.video_path for s in job.scenes if s.video_path])
            return

        # 2. Gem & Hesap Erişim Kontrolü
        gem_id = extract_gem_id_from_url(job.gem_url)
        gem_name = KNOWN_GEMS.get(gem_id, {}).get("name", gem_id or "Özel Gem")
        self._log(f"🔍 [İş #{job.job_id}] Gem erişimi kontrol ediliyor: {gem_name} ({gem_id})")

        # Aktif profil ve kota/erişim kontrolü
        active_profile = self.profile_manager.get_active_profile()
        profile_key = active_profile.profile_key if active_profile else "Default"

        if not active_profile.is_currently_usable():
            self._log(f"⏳ Profil ({profile_key}) şu an kullanımda değil veya kotada. Bekleme moduna alınıyor.")
            cooldown_hours = float(self.config.get("quota_cooldown_hours", 5.0))
            next_retry = datetime.now(timezone.utc) + timedelta(hours=cooldown_hours)
            err_info = ErrorInfo(
                category=ErrorCategory.QUOTA_EXCEEDED,
                raw_message="Profil kota sınırında",
                human_message=f"Hesap ({profile_key}) kullanım sınırında. Kota sıfırlandığında devam edecek.",
                is_retryable=True,
                suggested_backoff_seconds=int(cooldown_hours * 3600),
                requires_user_intervention=False,
            )
            self.job_queue.mark_waiting(job.job_id, next_retry.isoformat(), err_info)
            return

        # Gem erişim doğrulaması (Bilinen yetki / izin kontrolü)
        gem_accessible, access_reason = self.profile_manager.check_gem_access(profile_key, job.gem_url)
        if not gem_accessible:
            self._log(f"🛑 [İş #{job.job_id}] Gem erişim engeli: {access_reason}")
            err_info = ErrorInfo(
                category=ErrorCategory.GEM_ACCESS_DENIED,
                raw_message=access_reason,
                human_message=access_reason,
                is_retryable=False,
                suggested_backoff_seconds=0,
                requires_user_intervention=True,
            )
            self.job_queue.mark_needs_attention(job.job_id, err_info)
            return

        # 3. İşi Yürüt
        run_cfg = dict(self.config)
        run_cfg.update({
            "job_id": job.job_id,
            "brief": job.brief,
            "scenario": job.scenario,
            "content_type": job.content_type,
            "video_format": job.video_format,
            "scene_count": job.scene_count,
            "seconds_per_scene": job.seconds_per_scene,
            "voiceover": job.voiceover,
            "music": job.music,
            "editing_notes": job.editing_notes,
            "extra_notes": job.extra_notes,
            "target_folder_id": job.target_folder_id,
            "gem_url": job.gem_url,
            "brand_name": job.brand_name,
            "project_name": job.project_name,
            "dry_run": job.dry_run,
            "output_dir": job.output_dir,
        })

        # Kuyruk ilerlemesini kaydeden logger wrapper
        def workflow_log(msg: str) -> None:
            self._log(f"[İş #{job.job_id}] {msg}")

        try:
            report = run_content_workflow(
                cfg=run_cfg,
                refs=job.reference_images,
                log=workflow_log,
                provider=custom_provider,
                job_queue=self.job_queue,
            )

            # Rapor sonucuna göre kuyruk durumunu güncelle
            self._handle_workflow_report(job, report, profile_key)

        except Exception as exc:
            self._handle_workflow_exception(job, exc, profile_key)

    def _check_already_completed_on_disk(self, job: JobOrder) -> bool:
        """Çıktı klasöründe teslimat kanıtı veya tüm sahne videoları mevcut mu?"""
        out_dir = Path(job.output_dir) if job.output_dir else (APP_DIR / "outputs" / job.brand_name)
        if not out_dir.exists():
            return False

        delivered_marker = out_dir / ".delivered"
        full_video = out_dir / "03_Videolar" / "tam_video.mp4"

        # Tüm sahnelerin videoları diskte mevcut mu?
        all_scenes_present = bool(job.scenes)
        for sc in job.scenes:
            scene_vid = out_dir / "03_Videolar" / f"sahne_{sc.index}.mp4"
            if scene_vid.exists() and scene_vid.stat().st_size > 50000:
                sc.video_path = str(scene_vid)
                sc.status = "video_done"
            else:
                all_scenes_present = False

        has_full_video = full_video.exists() and full_video.stat().st_size > 100000
        has_delivered = delivered_marker.exists()

        if (has_full_video or has_delivered) and all_scenes_present:
            return True
        return False

    def _handle_workflow_report(self, job: JobOrder, report: WorkflowReport, profile_key: str) -> None:
        """Workflow tamamlandıktan sonra sonuç raporunu işler ve kuyruğu günceller."""
        # İlerlemeyi veritabanına kaydet
        video_paths = [sc.video_path for sc in report.job.scenes if sc.video_path]
        self.job_queue.update_progress(
            job_id=job.job_id,
            scenes=report.job.scenes,
            last_step=report.stages[-1].name if report.stages else "completed",
            profile_used=profile_key,
            output_dir=report.job.output_dir,
        )

        has_failed = any(s.status in (StageStatus.FAILED, StageStatus.RATE_LIMITED) for s in report.stages)
        if (report.delivered or (job.dry_run and report.overall_status == "dry_run")) and not has_failed:
            self.job_queue.mark_completed(job.job_id, video_paths)
            self.profile_manager.mark_profile_success(profile_key)
            self._log(f"🎉 [İş #{job.job_id}] Başarıyla tamamlandı!")
            return

        # Teslim edilemediyse son aşama hatasını incele
        failed_stage = next((s for s in reversed(report.stages) if s.status in (StageStatus.FAILED, StageStatus.RATE_LIMITED)), None)
        err_message = failed_stage.message if failed_stage else "İş teslimi doğrulanamadı."

        if failed_stage and failed_stage.status == StageStatus.RATE_LIMITED:
            err_info = classify_error(RateLimitError(err_message), cfg=self.config)
        else:
            err_info = classify_error(err_message, cfg=self.config)
        self._apply_error_strategy(job, err_info, profile_key)

    def _handle_workflow_exception(self, job: JobOrder, exc: Exception, profile_key: str) -> None:
        """Beklenmeyen istisna oluştuğunda hatayı sınıflandırıp stratejiyi uygular."""
        err_info = classify_error(exc, cfg=self.config)
        self._apply_error_strategy(job, err_info, profile_key)

    def _apply_error_strategy(self, job: JobOrder, err_info: ErrorInfo, profile_key: str) -> None:
        """Hata kategorisine göre yeniden deneme, bekleme veya müdahale durumuna alır."""
        cat = err_info.category
        self._log(f"⚠️ [İş #{job.job_id}] Hata sınıfı: {cat.value} | Açıklama: {err_info.human_message}")

        if cat == ErrorCategory.QUOTA_EXCEEDED:
            # Kota bekleme süresi: config'den dinamik (default 5 saat)
            cooldown_hours = float(self.config.get("quota_cooldown_hours", 5.0))
            cooldown_seconds = int(cooldown_hours * 3600)
            if err_info.suggested_backoff_seconds and err_info.suggested_backoff_seconds > 60:
                cooldown_seconds = err_info.suggested_backoff_seconds

            next_retry = datetime.now(timezone.utc) + timedelta(seconds=cooldown_seconds)
            self.profile_manager.mark_rate_limited(profile_key, cooldown_seconds=cooldown_seconds)
            self.job_queue.mark_waiting(job.job_id, next_retry.isoformat(), err_info)
            self._log(
                f"⏳ [İş #{job.job_id}] Gemini kotası doldu. {cooldown_hours:.1f} saat bekleme süresi planlandı "
                f"(Yeniden deneme zamanı: {next_retry.strftime('%Y-%m-%d %H:%M:%S UTC')})."
            )

        elif cat == ErrorCategory.HIGH_TRAFFIC_CAPACITY:
            # Sunucu yoğunluğu: kısa süreli bekleme (30 sn - 2 dk)
            backoff_sec = int(self.config.get("traffic_retry_seconds", 45))
            next_retry = datetime.now(timezone.utc) + timedelta(seconds=backoff_sec)
            self.job_queue.mark_waiting(job.job_id, next_retry.isoformat(), err_info)
            self._log(f"🔄 [İş #{job.job_id}] Sunucu yoğunluğu. {backoff_sec} saniye sonra tekrar denenecek.")

        elif cat in (ErrorCategory.NETWORK_CDP_DISCONNECTED, ErrorCategory.BROWSER_CRASHED, ErrorCategory.TEMPORARY_SERVER_ERROR):
            # Geçici bağlantı veya tarayıcı çökmesi: Max retries kontrolü
            max_retries = int(self.config.get("max_retries_per_job", 3))
            if job.retry_count < max_retries:
                backoff_sec = int(self.config.get("retry_backoff_base_seconds", 30)) * (2 ** job.retry_count)
                next_retry = datetime.now(timezone.utc) + timedelta(seconds=backoff_sec)
                self.job_queue.mark_waiting(job.job_id, next_retry.isoformat(), err_info)
                self._log(
                    f"🔄 [İş #{job.job_id}] Geçici bağlantı/çökme hatası ({job.retry_count + 1}/{max_retries}). "
                    f"{backoff_sec} sn sonra tekrar denenecek."
                )
            else:
                self.job_queue.mark_failed(job.job_id, err_info)
                self._log(f"❌ [İş #{job.job_id}] Maksimum deneme sayısına ulaşıldı ({max_retries}). İş başarısız.")

        elif cat in (
            ErrorCategory.SESSION_EXPIRED,
            ErrorCategory.GEM_ACCESS_DENIED,
            ErrorCategory.CONTENT_POLICY_REFUSAL,
            ErrorCategory.NEEDS_USER_INTERVENTION,
        ):
            # İnsan müdahalesi veya oturum yetkisi gerektiren durumlar (sonsuz döngü engellenir)
            self.job_queue.mark_needs_attention(job.job_id, err_info)
            self._log(
                f"🛑 [İş #{job.job_id}] Kullanıcı müdahalesi gerekli: {err_info.human_message}"
            )

        else:
            # Diğer veya bilinmeyen hatalar
            max_retries = int(self.config.get("max_retries_per_job", 3))
            if job.retry_count < max_retries:
                backoff_sec = 60
                next_retry = datetime.now(timezone.utc) + timedelta(seconds=backoff_sec)
                self.job_queue.mark_waiting(job.job_id, next_retry.isoformat(), err_info)
            else:
                self.job_queue.mark_failed(job.job_id, err_info)
