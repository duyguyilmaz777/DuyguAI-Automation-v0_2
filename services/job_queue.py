from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from models.job import JobOrder, JobStatus, SceneItem
from services.error_classifier import ErrorCategory, ErrorInfo
from services.profile_manager import extract_gem_id_from_url

QUEUE_DB_PATH = Path(__file__).resolve().parent.parent / "jobs" / "queue.db"


class JobQueue:
    """SQLite tabanlı, kilit korumalı ve elektrik/çökme kurtarmalı kalıcı iş kuyruğu."""

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else QUEUE_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    brief TEXT,
                    scenario TEXT,
                    content_type TEXT,
                    video_format TEXT,
                    scene_count INTEGER DEFAULT 1,
                    seconds_per_scene INTEGER DEFAULT 5,
                    voiceover INTEGER DEFAULT 0,
                    music INTEGER DEFAULT 0,
                    editing_notes TEXT,
                    extra_notes TEXT,
                    target_folder_id TEXT,
                    brand_name TEXT,
                    project_name TEXT,
                    gem_url TEXT,
                    gem_id TEXT,
                    reference_images TEXT,
                    output_dir TEXT,
                    status TEXT NOT NULL,
                    completed_scenes TEXT,
                    pending_scenes TEXT,
                    generated_video_paths TEXT,
                    retry_count INTEGER DEFAULT 0,
                    max_retries INTEGER DEFAULT 3,
                    last_error TEXT,
                    last_error_category TEXT,
                    last_profile_used TEXT,
                    last_successful_step TEXT,
                    next_retry_at TEXT,
                    is_locked INTEGER DEFAULT 0,
                    locked_by TEXT,
                    locked_at TEXT,
                    dry_run INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS queue_state (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
                CREATE INDEX IF NOT EXISTS idx_jobs_next_retry ON jobs(next_retry_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_locked ON jobs(is_locked);
                """
            )
            try:
                conn.execute("ALTER TABLE jobs ADD COLUMN dry_run INTEGER DEFAULT 0")
            except Exception:
                pass

    # -------------------------------------------------------------------------
    # Kuyruk Denetimi (Duraklat / Devam Et)
    # -------------------------------------------------------------------------
    def pause_queue(self) -> None:
        with self._get_connection() as conn:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO queue_state (key, value, updated_at) VALUES ('is_paused', '1', ?) "
                "ON CONFLICT(key) DO UPDATE SET value='1', updated_at=?",
                (now, now),
            )

    def resume_queue(self) -> None:
        with self._get_connection() as conn:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO queue_state (key, value, updated_at) VALUES ('is_paused', '0', ?) "
                "ON CONFLICT(key) DO UPDATE SET value='0', updated_at=?",
                (now, now),
            )

    def is_queue_paused(self) -> bool:
        with self._get_connection() as conn:
            row = conn.execute("SELECT value FROM queue_state WHERE key = 'is_paused'").fetchone()
            return bool(row and row["value"] == "1")

    # -------------------------------------------------------------------------
    # İş Ekleme & Sorgulama
    # -------------------------------------------------------------------------
    def enqueue_job(self, job: JobOrder) -> str:
        """Yeni bir işi kalıcı veritabanına ekler veya mevcut işi günceller."""
        now = datetime.now(timezone.utc).isoformat()
        gem_id = extract_gem_id_from_url(job.gem_url)

        completed_indices = [s.index for s in job.scenes if s.status in ("video_done", "completed")]
        pending_indices = [s.index for s in job.scenes if s.index not in completed_indices]

        completed_json = json.dumps([s.to_dict() for s in job.scenes if s.index in completed_indices], ensure_ascii=False)
        pending_json = json.dumps([s.to_dict() for s in job.scenes if s.index in pending_indices], ensure_ascii=False)
        refs_json = json.dumps(job.reference_images or [], ensure_ascii=False)
        vids_json = json.dumps([s.video_path for s in job.scenes if s.video_path], ensure_ascii=False)

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, brief, scenario, content_type, video_format, scene_count,
                    seconds_per_scene, voiceover, music, editing_notes, extra_notes,
                    target_folder_id, brand_name, project_name, gem_url, gem_id,
                    reference_images, output_dir, status, completed_scenes, pending_scenes,
                    generated_video_paths, retry_count, max_retries, last_error,
                    last_error_category, last_profile_used, last_successful_step,
                    next_retry_at, is_locked, locked_by, locked_at, dry_run, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    brief=excluded.brief,
                    scenario=excluded.scenario,
                    content_type=excluded.content_type,
                    video_format=excluded.video_format,
                    scene_count=excluded.scene_count,
                    seconds_per_scene=excluded.seconds_per_scene,
                    output_dir=excluded.output_dir,
                    dry_run=excluded.dry_run,
                    updated_at=excluded.updated_at
                """,
                (
                    job.job_id, job.brief, job.scenario, job.content_type, job.video_format,
                    job.scene_count, job.seconds_per_scene, int(job.voiceover), int(job.music),
                    job.editing_notes, job.extra_notes, job.target_folder_id, job.brand_name,
                    job.project_name, job.gem_url, gem_id, refs_json, job.output_dir,
                    job.status or JobStatus.PENDING.value, completed_json, pending_json,
                    vids_json, job.retry_count, job.max_retries, job.last_error,
                    job.last_error_category, job.last_profile_used, job.last_successful_step,
                    job.next_retry_at, int(job.is_locked), job.locked_by, job.locked_at,
                    int(job.dry_run), job.created_at or now, now,
                ),
            )
        return job.job_id

    def get_job(self, job_id: str) -> JobOrder | None:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row:
                return None
            return self._row_to_job(row)

    def _row_to_job(self, row: sqlite3.Row) -> JobOrder:
        refs = json.loads(row["reference_images"] or "[]")
        completed = json.loads(row["completed_scenes"] or "[]")
        pending = json.loads(row["pending_scenes"] or "[]")

        # Sahne listesini yeniden inşa et: Tamamlananlar bekleyenlerin üzerine yazar
        all_scenes_dict = {}
        for item in pending + completed:
            idx = item.get("index")
            if idx is not None:
                all_scenes_dict[idx] = SceneItem(**item)

        scenes = [all_scenes_dict[k] for k in sorted(all_scenes_dict.keys())]
        is_dry_run = bool(row["dry_run"]) if "dry_run" in row.keys() else False

        job = JobOrder(
            job_id=row["job_id"],
            brief=row["brief"] or "",
            scenario=row["scenario"] or "",
            content_type=row["content_type"] or "",
            video_format=row["video_format"] or "9:16",
            scene_count=row["scene_count"] or 1,
            seconds_per_scene=row["seconds_per_scene"] or 5,
            voiceover=bool(row["voiceover"]),
            music=bool(row["music"]),
            editing_notes=row["editing_notes"] or "",
            extra_notes=row["extra_notes"] or "",
            target_folder_id=row["target_folder_id"] or "",
            gem_url=row["gem_url"] or "",
            reference_images=refs,
            brand_name=row["brand_name"] or "DuyguAI",
            project_name=row["project_name"] or "Duygu AI Studio",
            dry_run=is_dry_run,
            output_dir=row["output_dir"] or "",
            scenes=scenes,
            status=row["status"],
            retry_count=row["retry_count"] or 0,
            max_retries=row["max_retries"] or 3,
            last_error=row["last_error"] or "",
            last_error_category=row["last_error_category"] or "",
            last_profile_used=row["last_profile_used"] or "",
            last_successful_step=row["last_successful_step"] or "",
            next_retry_at=row["next_retry_at"],
            is_locked=bool(row["is_locked"]),
            locked_by=row["locked_by"] or "",
            locked_at=row["locked_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        return job

    # -------------------------------------------------------------------------
    # Atomik Kilit ve İş Alma (Concurrency Guard & Stale Lock Recovery)
    # -------------------------------------------------------------------------
    def acquire_next_job(self, worker_id: str, stale_lock_seconds: int = 900) -> JobOrder | None:
        """Kuyruktan bir sonraki uygun işi atomik olarak kilitler ve döndürür."""
        if self.is_queue_paused():
            return None

        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            # 1. Bayat kilitleri temizle (Çökmüş worker'ların kilitlerini kurtar)
            conn.execute(
                """
                UPDATE jobs SET is_locked = 0, locked_by = NULL, locked_at = NULL,
                                status = 'pending', updated_at = ?
                WHERE is_locked = 1 AND status = 'running'
                  AND locked_at IS NOT NULL
                  AND (strftime('%s', ?) - strftime('%s', locked_at)) > ?
                """,
                (now, now, stale_lock_seconds),
            )

            # 2. Çalıştırılabilir işi seç:
            # - Durumu 'pending' veya 'waiting'
            # - next_retry_at boş veya geçmişte kalmış
            # - Kilitli değil
            candidate = conn.execute(
                """
                SELECT job_id FROM jobs
                WHERE is_locked = 0
                  AND (
                        status = 'pending'
                        OR (status = 'waiting' AND (next_retry_at IS NULL OR next_retry_at <= ?))
                      )
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (now,),
            ).fetchone()

            if not candidate:
                return None

            job_id = candidate["job_id"]

            # 3. Atomik kilitleme
            updated = conn.execute(
                """
                UPDATE jobs SET
                    is_locked = 1,
                    locked_by = ?,
                    locked_at = ?,
                    status = 'running',
                    updated_at = ?
                WHERE job_id = ? AND is_locked = 0
                """,
                (worker_id, now, now, job_id),
            ).rowcount

            if updated == 0:
                # Eşzamanlı başka bir worker kaptı
                return None

            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            return self._row_to_job(row) if row else None

    def release_job_lock(self, job_id: str, worker_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE jobs SET is_locked = 0, locked_by = NULL, locked_at = NULL, updated_at = ?
                WHERE job_id = ? AND locked_by = ?
                """,
                (now, job_id, worker_id),
            )

    # -------------------------------------------------------------------------
    # Durum ve İlerleme Güncellemeleri
    # -------------------------------------------------------------------------
    def update_progress(
        self,
        job_id: str,
        scenes: list[SceneItem],
        last_step: str,
        profile_used: str = "",
        output_dir: str = "",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        completed_indices = [s.index for s in scenes if s.status in ("video_done", "completed")]
        pending_indices = [s.index for s in scenes if s.index not in completed_indices]

        comp_json = json.dumps([s.to_dict() for s in scenes if s.index in completed_indices], ensure_ascii=False)
        pend_json = json.dumps([s.to_dict() for s in scenes if s.index in pending_indices], ensure_ascii=False)
        vids_json = json.dumps([s.video_path for s in scenes if s.video_path], ensure_ascii=False)

        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE jobs SET
                    completed_scenes = ?,
                    pending_scenes = ?,
                    generated_video_paths = ?,
                    last_successful_step = ?,
                    last_profile_used = COALESCE(NULLIF(?, ''), last_profile_used),
                    output_dir = COALESCE(NULLIF(?, ''), output_dir),
                    updated_at = ?
                WHERE job_id = ?
                """,
                (comp_json, pend_json, vids_json, last_step, profile_used, output_dir, now, job_id),
            )

    def mark_completed(self, job_id: str, video_paths: list[str] | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        vids_json = json.dumps(video_paths or [], ensure_ascii=False) if video_paths is not None else None
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE jobs SET
                    status = 'completed',
                    is_locked = 0,
                    locked_by = NULL,
                    locked_at = NULL,
                    generated_video_paths = COALESCE(?, generated_video_paths),
                    last_error = NULL,
                    last_error_category = NULL,
                    next_retry_at = NULL,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (vids_json, now, job_id),
            )

    def mark_waiting(self, job_id: str, next_retry_at: str, error_info: ErrorInfo | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        err_msg = error_info.human_message if error_info else "Bekleme moduna alındı"
        err_cat = error_info.category.value if error_info else ErrorCategory.QUOTA_EXCEEDED.value
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE jobs SET
                    status = 'waiting',
                    is_locked = 0,
                    locked_by = NULL,
                    locked_at = NULL,
                    next_retry_at = ?,
                    last_error = ?,
                    last_error_category = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (next_retry_at, err_msg, err_cat, now, job_id),
            )

    def mark_needs_attention(self, job_id: str, error_info: ErrorInfo | None = None, message: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        err_msg = message or (error_info.human_message if error_info else "Kullanıcı müdahalesi gerekli.")
        err_cat = error_info.category.value if error_info else ErrorCategory.NEEDS_USER_INTERVENTION.value
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE jobs SET
                    status = 'needs_attention',
                    is_locked = 0,
                    locked_by = NULL,
                    locked_at = NULL,
                    last_error = ?,
                    last_error_category = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (err_msg, err_cat, now, job_id),
            )

    def mark_failed(self, job_id: str, error_info: ErrorInfo | None = None, message: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        err_msg = message or (error_info.human_message if error_info else "İş başarısız oldu.")
        err_cat = error_info.category.value if error_info else ErrorCategory.UNKNOWN.value
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE jobs SET
                    status = 'failed',
                    is_locked = 0,
                    locked_by = NULL,
                    locked_at = NULL,
                    retry_count = retry_count + 1,
                    last_error = ?,
                    last_error_category = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (err_msg, err_cat, now, job_id),
            )

    # -------------------------------------------------------------------------
    # Sistem Başlatma Kurtarması (Auto Recovery on App Start / Reboot)
    # -------------------------------------------------------------------------
    def recover_interrupted_jobs(self) -> int:
        """Uygulama yeniden başladığında kilitli veya yarıda kalmış işleri kurtarır."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            # Yarıda kalmış (running olarak kaydedilmiş) işleri beklemeye veya pending'e al
            cursor = conn.execute(
                """
                UPDATE jobs SET
                    is_locked = 0,
                    locked_by = NULL,
                    locked_at = NULL,
                    status = CASE
                        WHEN next_retry_at IS NOT NULL AND next_retry_at > ? THEN 'waiting'
                        ELSE 'pending'
                    END,
                    updated_at = ?
                WHERE is_locked = 1 OR status = 'running'
                """,
                (now, now),
            )
            return cursor.rowcount

    # -------------------------------------------------------------------------
    # Listeleme ve İstatistikler
    # -------------------------------------------------------------------------
    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_queue_summary(self) -> dict[str, Any]:
        with self._get_connection() as conn:
            counts = {
                "total": 0,
                "pending": 0,
                "running": 0,
                "completed": 0,
                "failed": 0,
                "needs_attention": 0,
                "waiting": 0,
                "paused": 0,
            }
            for row in conn.execute("SELECT status, count(*) as cnt FROM jobs GROUP BY status"):
                st = row["status"]
                if st in counts:
                    counts[st] = row["cnt"]
                counts["total"] += row["cnt"]

            next_job = conn.execute(
                "SELECT next_retry_at FROM jobs WHERE status = 'waiting' AND next_retry_at IS NOT NULL ORDER BY next_retry_at ASC LIMIT 1"
            ).fetchone()
            earliest_retry = next_job["next_retry_at"] if next_job else None

            return {
                "counts": counts,
                "is_paused": self.is_queue_paused(),
                "earliest_retry_at": earliest_retry,
            }
