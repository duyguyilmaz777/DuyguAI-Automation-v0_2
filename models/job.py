from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


CONTENT_TYPES = [
    "ürün reklamı",
    "marka tanıtımı",
    "sosyal medya Reels",
    "klinik videosu",
    "animasyon",
    "diğer",
]

VIDEO_FORMATS = ["9:16", "16:9", "1:1"]

PROJECT_DUYGU = "Duygu AI Studio"
PROJECT_IKIZLER = "İkizler Masal Durağı"
PROJECTS = [PROJECT_DUYGU, PROJECT_IKIZLER]

DEFAULT_GEM_URLS = {
    PROJECT_DUYGU: "https://gemini.google.com/gem/af11977bbdce",
    PROJECT_IKIZLER: "https://gemini.google.com/gem/8e63671e6329",
}


class StageStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    PENDING = "pending"
    SKIPPED = "skipped"
    UNSUPPORTED = "unsupported"
    DRY_RUN = "dry_run"
    RATE_LIMITED = "rate_limited"
    NEEDS_ATTENTION = "needs_attention"
    PAUSED = "paused"
    WAITING = "waiting"


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_ATTENTION = "needs_attention"
    PAUSED = "paused"
    WAITING = "waiting"


@dataclass
class SceneItem:
    index: int
    title: str = ""
    description: str = ""
    duration_seconds: int = 5
    voiceover_text: str = ""
    image_prompt: str = ""
    video_prompt: str = ""
    image_path: str = ""
    video_path: str = ""
    status: str = "pending"  # pending, image_done, video_done, failed, skipped

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StageResult:
    name: str
    status: StageStatus
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "message": self.message,
            "details": self.details,
        }


@dataclass
class JobOrder:
    job_id: str
    brief: str
    scenario: str
    content_type: str
    video_format: str
    scene_count: int
    seconds_per_scene: int
    voiceover: bool
    music: bool
    editing_notes: str
    extra_notes: str
    target_folder_id: str
    gem_url: str
    reference_images: list[str]
    brand_name: str = "DuyguAI"
    project_name: str = PROJECT_DUYGU
    delivery_mode: str = "both"  # local, drive, both
    output_dir: str = ""
    scenes: list[SceneItem] = field(default_factory=list)
    generated_prompt: str = ""
    scene_plan: str = ""
    scene_plan_source: str = "none"
    dry_run: bool = True
    instagram_post: dict[str, Any] = field(default_factory=dict)
    cover_image_path: str = ""
    status: str = "pending"
    retry_count: int = 0
    max_retries: int = 3
    last_error: str = ""
    last_error_category: str = ""
    last_profile_used: str = ""
    last_successful_step: str = ""
    next_retry_at: str | None = None
    is_locked: bool = False
    locked_by: str = ""
    locked_at: str | None = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data


@dataclass
class WorkflowReport:
    job: JobOrder
    stages: list[StageResult] = field(default_factory=list)
    overall_status: str = "pending"
    delivered: bool = False
    production_requested: bool = False
    video_src_detected: bool = False

    def add(self, result: StageResult) -> StageResult:
        self.stages.append(result)
        return result

    def failed_stages(self) -> list[StageResult]:
        return [s for s in self.stages if s.status == StageStatus.FAILED]

    def pending_or_unsupported(self) -> list[StageResult]:
        return [
            s
            for s in self.stages
            if s.status in (StageStatus.PENDING, StageStatus.UNSUPPORTED)
        ]

    def finalize(self) -> None:
        if any(s.status == StageStatus.RATE_LIMITED for s in self.stages):
            self.overall_status = "rate_limited"
            self.delivered = False
            return
        if self.failed_stages():
            self.overall_status = "failed"
            self.delivered = False
            return
        if self.job.dry_run or not self.production_requested:
            self.overall_status = "dry_run"
            self.delivered = False
            return
        if not self.delivered:
            self.overall_status = "incomplete"
            return
        self.overall_status = "success"

    def summary_lines(self) -> list[str]:
        lines = [
            f"İş emri: {self.job.job_id}",
            f"Genel durum: {self.overall_status}",
            f"Teslim doğrulandı: {'evet' if self.delivered else 'hayır'}",
            f"Güvenli test (dry_run): {'evet' if self.job.dry_run else 'hayır'}",
            "Aşamalar:",
        ]
        for stage in self.stages:
            lines.append(f"  - [{stage.status.value}] {stage.name}: {stage.message}")
        return lines

    def to_dict(self) -> dict[str, Any]:
        return {
            "job": self.job.to_dict(),
            "overall_status": self.overall_status,
            "delivered": self.delivered,
            "production_requested": self.production_requested,
            "video_src_detected": self.video_src_detected,
            "stages": [s.to_dict() for s in self.stages],
        }
