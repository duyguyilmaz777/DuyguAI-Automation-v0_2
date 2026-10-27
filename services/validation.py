from __future__ import annotations

from dataclasses import dataclass

from models.job import CONTENT_TYPES, VIDEO_FORMATS, PROJECT_DUYGU, PROJECT_IKIZLER
from services.files import extract_drive_folder_id


class ValidationError(ValueError):
    def __init__(self, errors: list[str], warnings: list[str] | None = None):
        self.errors = errors
        self.warnings = warnings or []
        super().__init__(" | ".join(errors))


@dataclass
class ValidationResult:
    errors: list[str]
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_job_inputs(
    *,
    brief: str = "",
    scenario: str = "",
    extra_notes: str = "",
    content_type: str = "ürün reklamı",
    video_format: str = "9:16",
    scene_count: int | str = 1,
    seconds_per_scene: int | str = 5,
    gem_url: str = "",
    target_folder_id: str = "",
    root_drive_folder: str = "",
    dry_run: bool = True,
    reference_images: list | None = None,
    project_name: str = PROJECT_DUYGU,
) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    brief = (brief or "").strip()
    scenario = (scenario or "").strip()
    extra_notes = (extra_notes or "").strip()
    refs = list(reference_images or [])

    if not brief and not scenario and not extra_notes:
        errors.append(
            "İçerik briefi, hazır senaryo veya ek notlardan en az biri gerekli. Üretim başlatılmadı."
        )
    if not brief:
        warnings.append("İçerik açıklaması/brief boş. Yalnızca senaryo veya ek notlarla devam edilecek.")
    if not scenario:
        warnings.append(
            "Hazır senaryo yok. Ayrı senaryo üretimi otomatik parse edilmez; brief üretim isteğine eklenecek."
        )
    if not refs:
        warnings.append("Referans görseli seçilmedi. İş akışı görselsiz devam edebilir.")

    if project_name == PROJECT_DUYGU and content_type not in CONTENT_TYPES:
        errors.append(f"Geçersiz içerik türü: {content_type}")
    if video_format not in VIDEO_FORMATS:
        errors.append(f"Geçersiz video formatı: {video_format}")

    try:
        scenes = int(scene_count)
        if scenes < 1 or scenes > 20:
            errors.append("Sahne sayısı 1 ile 20 arasında olmalı.")
    except (TypeError, ValueError):
        errors.append("Sahne sayısı tam sayı olmalı.")

    try:
        seconds = int(seconds_per_scene)
        if seconds < 1 or seconds > 60:
            errors.append("Sahne süresi 1 ile 60 saniye arasında olmalı.")
    except (TypeError, ValueError):
        errors.append("Sahne süresi tam sayı olmalı.")

    if not (gem_url or "").strip():
        errors.append("Gemini Gem URL'si gerekli.")

    effective_drive = extract_drive_folder_id(target_folder_id) or extract_drive_folder_id(root_drive_folder)
    if not effective_drive:
        if dry_run:
            warnings.append("Hedef Drive ana klasörü tanımlı değil; yerel arşivleme yapılacak.")
        else:
            errors.append(
                "Gerçek üretim için Ayarlar sekmesinde 'Ana Google Drive Klasörü' (URL veya ID) tanımlanmalıdır."
            )

    return ValidationResult(errors=errors, warnings=warnings)

