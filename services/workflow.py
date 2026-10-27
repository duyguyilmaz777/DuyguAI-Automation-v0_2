from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from models.job import (
    JobOrder,
    PROJECT_DUYGU,
    PROJECT_IKIZLER,
    SceneItem,
    StageResult,
    StageStatus,
    WorkflowReport,
)
from providers.base import (
    HighTrafficCapacityError,
    RateLimitError,
    SafetyRefusalError,
    TransientGeminiError,
    VideoPolicyRefusalError,
)
from providers.video_sources import is_new_http_video
from services.social_media import prepare_instagram_package
from services.prompt_builder import (
    build_production_prompt,
    build_scene_image_prompt,
    build_scene_video_prompt,
    build_script_breakdown_prompt,
    clean_model_response_text,
    has_scene_markers,
    is_animation_content_type,
    parse_scene_breakdown,
    revise_scene_video_prompt_for_safety,
    sanitize_topic_for_prompts,
    clean_voiceover_for_prompt,
)

from services.files import extract_drive_folder_id
from services.validation import ValidationError, validate_job_inputs


APP_DIR = Path(__file__).resolve().parent.parent
JOBS_DIR = APP_DIR / "jobs"
OUTPUTS_DIR = APP_DIR / "outputs"


def _bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "evet"}
    return bool(value)


def _sanitize_folder_name(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', "_", name).strip()
    return cleaned or "DuyguAI_Projesi"


def determine_brand_name(cfg: dict, brief: str = "", scenario: str = "") -> str:
    if cfg.get("brand_name"):
        return _sanitize_folder_name(cfg["brand_name"])

    if cfg.get("masal_topic"):
        words = re.findall(r'[a-zA-ZçğıöşüÇĞİÖŞÜ0-9]+', cfg["masal_topic"])
        if words:
            clean = _sanitize_folder_name("Masal_" + "_".join(w.capitalize() for w in words[:4]))
            if clean:
                return clean

    text = f"{brief} {scenario}".strip()
    words = re.findall(r'[a-zA-ZçğıöşüÇĞİÖŞÜ0-9]+', text)
    stopwords = {"ve", "ile", "için", "gibi", "bir", "bu", "şu", "da", "de", "düz", "icin", "icin"}
    meaningful = [w for w in words if w.lower() not in stopwords]

    if meaningful:
        slug = "_".join(w.capitalize() for w in meaningful[:3])
        clean = _sanitize_folder_name(slug)
        if clean:
            return clean

    src = cfg.get("source_folder") or ""
    if src:
        name = Path(src).name
        if name:
            clean = _sanitize_folder_name(name)
            if clean:
                return clean
    if cfg.get("project_name") == "İkizler Masal Durağı":
        return "Ikizler_Masal"
    return "DuyguAI_Kampanya"


def create_job_order(cfg: dict, refs: list) -> JobOrder:
    brief = (cfg.get("brief") or "").strip()
    scenario = (cfg.get("scenario") or "").strip()
    extra_notes = (cfg.get("extra_notes") or cfg.get("prompt") or "").strip()
    if not brief and not scenario and extra_notes:
        brief = extra_notes
        extra_notes = ""
    dry_run = _bool(cfg.get("dry_run"), True)
    brand = determine_brand_name(cfg, brief, scenario)
    scene_cnt = int(cfg.get("scene_count") or 1)
    sec_per_scene = int(cfg.get("seconds_per_scene") or 5)

    raw_target = (cfg.get("target_folder_id") or cfg.get("root_drive_folder") or "").strip()
    effective_target_id = extract_drive_folder_id(raw_target)

    custom_output_dir = (cfg.get("output_dir") or "").strip()
    job = JobOrder(
        job_id=str(uuid.uuid4())[:8],
        brief=brief,
        scenario=scenario,
        content_type=(cfg.get("content_type") or "ürün reklamı").strip(),
        video_format=(cfg.get("video_format") or "9:16").strip(),
        scene_count=scene_cnt,
        seconds_per_scene=sec_per_scene,
        voiceover=_bool(cfg.get("voiceover"), False),
        music=_bool(cfg.get("music"), False),
        editing_notes=(cfg.get("editing_notes") or "").strip(),
        extra_notes=extra_notes,
        target_folder_id=effective_target_id,
        gem_url=(cfg.get("gem_url") or "").strip(),
        reference_images=[str(p) for p in refs],
        brand_name=brand,
        project_name=(cfg.get("project_name") or "Duygu AI Studio").strip(),
        dry_run=dry_run,
        output_dir=custom_output_dir,
    )
    if cfg.get("scene_plan_source"):
        job.scene_plan_source = cfg["scene_plan_source"]
    elif scenario:
        job.scene_plan = scenario
        job.scene_plan_source = "user_provided"
    job.generated_prompt = build_production_prompt(job)
    return job


def setup_output_directories(job: JobOrder) -> dict[str, Path]:
    if job.output_dir and Path(job.output_dir).exists():
        base = Path(job.output_dir)
    else:
        brand = job.brand_name
        base = OUTPUTS_DIR / brand
        # Eğer bu marka/proje daha önce başarıyla tamamlanıp teslim edildiyse (.delivered varsa),
        # yeni üretimin temiz ve bağımsız olması için zaman damgası ekle
        if base.exists() and (base / ".delivered").exists():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            base = OUTPUTS_DIR / f"{brand}_{timestamp}"


    scenarios = base / "01_Senaryo"
    images = base / "02_Gorseller"
    videos = base / "03_Videolar"
    for d in (scenarios, images, videos):
        d.mkdir(parents=True, exist_ok=True)
    job.output_dir = str(base)
    return {
        "base": base,
        "scenarios": scenarios,
        "images": images,
        "videos": videos,
    }


def save_job(job: JobOrder, report: WorkflowReport | None = None) -> Path:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    path = JOBS_DIR / f"{job.job_id}.json"
    payload = report.to_dict() if report else {"job": job.to_dict()}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run_content_workflow(
    cfg: dict,
    refs: list,
    log: Callable[[str], None],
    provider=None,
    job_queue=None,
) -> WorkflowReport:
    """Tam otonom, aşamalı sıralı iş akışını (Pipeline) yürütür."""
    refs = [str(p) for p in (refs or [])]
    dry_run = _bool(cfg.get("dry_run"), True)

    raw_target = (cfg.get("target_folder_id") or cfg.get("root_drive_folder") or "").strip()
    effective_target_id = extract_drive_folder_id(raw_target)

    project_name = (cfg.get("project_name") or PROJECT_DUYGU).strip()
    default_content_type = "çocuk masalı" if project_name == PROJECT_IKIZLER else "ürün reklamı"
    validation = validate_job_inputs(
        brief=cfg.get("brief") or "",
        scenario=cfg.get("scenario") or "",
        extra_notes=cfg.get("extra_notes") or cfg.get("prompt") or "",
        content_type=cfg.get("content_type") or default_content_type,
        video_format=cfg.get("video_format") or "9:16",
        scene_count=cfg.get("scene_count") or 1,
        seconds_per_scene=cfg.get("seconds_per_scene") or 5,
        gem_url=cfg.get("gem_url") or "",
        target_folder_id=effective_target_id,
        root_drive_folder=(cfg.get("root_drive_folder") or "").strip(),
        dry_run=dry_run,
        reference_images=refs,
        project_name=project_name,
    )
    if not validation.ok:
        raise ValidationError(validation.errors, validation.warnings)

    job = create_job_order(cfg, refs)
    if cfg.get("job_id"):
        job.job_id = cfg["job_id"]
    dirs = setup_output_directories(job)
    report = WorkflowReport(job=job, production_requested=not job.dry_run)

    for warning in validation.warnings:
        log("UYARI: " + warning)

    report.add(
        StageResult(
            "validate",
            StageStatus.SUCCESS,
            "Girdiler doğrulandı, iş emri oluşturuldu.",
            {"warnings": validation.warnings, "job_id": job.job_id, "output_dir": job.output_dir},
        )
    )
    log(f"İş emri oluşturuldu: {job.job_id} | Hedef Klasör: outputs/{dirs['base'].name}")

    owns_provider = provider is None
    try:
        if owns_provider:
            from providers.gemini import GeminiWebProvider

            provider = GeminiWebProvider(cfg, log)

        # Chrome/Gemini bağlantısı
        try:
            provider.connect()
            old_sources = provider.snapshot_videos()
            log(f"Üretim öncesi {len(old_sources)} video kaynağı kaydedildi.")
            if job.reference_images:
                ref_label = "karakter/masal referans görseli" if (project_name == PROJECT_IKIZLER or is_animation_content_type(job)) else "referans görseli"
                log(f"{len(job.reference_images)} {ref_label} ekleniyor…")
                provider.attach_references(job.reference_images)
        except Exception as exc:
            report.add(
                StageResult(
                    "gemini_connect",
                    StageStatus.FAILED,
                    f"Gemini bağlantısı veya referans ekleme başarısız: {exc}",
                )
            )
            report.finalize()
            save_job(job, report)
            _log_report(log, report)
            return report

        # ------------------------------------------------------------------
        # GÜVENLİ TEST MODU (DRY_RUN): Dış istek veya yükleme yapılmaz
        # ------------------------------------------------------------------
        if job.dry_run:
            log("GÜVENLİ TEST: Üretim isteği Gemini'ye gönderilmedi.")
            log("GÜVENLİ TEST: Google Drive yüklemesi yapılmadı.")

            # Senaryo planlaması yerel olarak ayrıştırılır veya mevcut tam plan korunur
            existing_plan_path = dirs["scenarios"] / "sahne_plani.json"
            existing_txt_path = dirs["scenarios"] / "tam_senaryo.txt"
            if existing_plan_path.exists() and existing_plan_path.stat().st_size > 50:
                try:
                    raw_json = json.loads(existing_plan_path.read_text(encoding="utf-8"))
                    if isinstance(raw_json, list) and len(raw_json) >= job.scene_count:
                        job.scenes = [SceneItem(**item) for item in raw_json]
                        job.scene_plan = existing_txt_path.read_text(encoding="utf-8") if existing_txt_path.exists() else ""
                        job.scene_plan_source = "reused_existing"
                except Exception:
                    pass

            if not job.scenes:
                raw_text = job.scenario or f"Sahne 1: {job.brief}"
                job.scenes = parse_scene_breakdown(
                    raw_text,
                    default_count=job.scene_count,
                    default_seconds=job.seconds_per_scene,
                    fallback_prompt=job.extra_notes or job.brief,
                )
                job.scene_plan = raw_text
                (dirs["scenarios"] / "tam_senaryo.txt").write_text(job.scene_plan, encoding="utf-8")
                (dirs["scenarios"] / "sahne_plani.json").write_text(
                    json.dumps([s.to_dict() for s in job.scenes], ensure_ascii=False, indent=2), encoding="utf-8"
                )

            report.add(
                StageResult(
                    "script_breakdown",
                    StageStatus.DRY_RUN,
                    f"Güvenli test: {len(job.scenes)} sahnelik plan simüle edildi.",
                    {"scene_count": len(job.scenes)},
                )
            )
            report.add(
                StageResult(
                    "scene_images",
                    StageStatus.DRY_RUN,
                    f"Güvenli test: {len(job.scenes)} sahne görseli simüle edildi.",
                )
            )
            report.add(
                StageResult(
                    "scene_videos",
                    StageStatus.DRY_RUN,
                    "Güvenli test: video üretim isteği gönderilmedi.",
                    {"requested_scenes": job.scene_count},
                )
            )
            report.add(
                StageResult(
                    "drive_upload",
                    StageStatus.DRY_RUN,
                    "Güvenli test: Drive aktarımı yapılmadı.",
                )
            )
            report.add(
                StageResult(
                    "verify_outputs",
                    StageStatus.DRY_RUN,
                    "Güvenli test: çıktı doğrulaması atlandı.",
                )
            )
            try:
                prepare_instagram_package(job, dirs, log)
            except Exception as ig_err:
                log(f"Instagram paylaşım paketi notu (dry run): {ig_err}")

            report.finalize()
            save_job(job, report)
            _log_report(log, report)
            return report

        # ------------------------------------------------------------------
        # CANLI MOD: 1. AŞAMA - Senaryo ve Sahne Ayrıştırma (Tam Otomatik)
        # ------------------------------------------------------------------
        # Gemini sohbet oturumunu temiz ve bağımsız bir iş için sıfırla
        if hasattr(provider, "reset_chat_session"):
            log("Gemini sohbet oturumu temizleniyor (yeni iş emri başlatılıyor)…")
            try:
                provider.reset_chat_session()
            except Exception as reset_err:
                log(f"Sohbet oturumu sıfırlama notu: {reset_err}")

        log("=== [1. AŞAMA] Senaryo ve Sahne Ayrıştırma Başlatılıyor ===")
        existing_plan_path = dirs["scenarios"] / "sahne_plani.json"
        existing_txt_path = dirs["scenarios"] / "tam_senaryo.txt"
        loaded_existing_plan = False
        # 1. Klasördeki mevcut senaryo planını kontrol et
        if existing_plan_path.exists() and existing_plan_path.stat().st_size > 50:
            try:
                raw_json = json.loads(existing_plan_path.read_text(encoding="utf-8"))
                if isinstance(raw_json, list) and len(raw_json) > 0:
                    # Eski hatalı "Otomatik sahne" veya ham brief taslağı değilse doğrudan kullan
                    has_dummy_desc = any("otomatik sahne" in (item.get("description", "") or "").lower() for item in raw_json)
                    has_fallback_template = any(
                        "giriş ve meraklı karşılaşma" in (item.get("title", "") or "").lower()
                        or "keşif ve eğlenceli oyun" in (item.get("title", "") or "").lower()
                        or "ürün tanıtımı ve estetik sunum" in (item.get("title", "") or "").lower()
                        for item in raw_json
                    )
                    has_raw_brief = any(
                        (item.get("image_prompt", "") or "").strip() == (job.brief or "").strip()
                        or (item.get("description", "") or "").strip() == (job.brief or "").strip()
                        for item in raw_json
                    )
                    has_meta_err = any(
                        any(bad in (item.get("image_prompt", "") or "").lower() for bad in [
                            "ugc içeriği", "içeriği üret", "video üret", "senaryo yaz", "dil modeli", "programlanmadım", "yardımcı olamıyorum"
                        ])
                        for item in raw_json
                    )
                    has_generic_vid = any(
                        (item.get("video_prompt", "") or "").strip() in ["Smooth cinematic camera movement", "Smooth cinematic movement", ""]
                        for item in raw_json
                    )
                    is_valid_plan = not (has_dummy_desc or has_fallback_template or has_raw_brief or has_meta_err or has_generic_vid)
                    if is_valid_plan or (job.scene_plan_source == "reused_existing"):
                        job.scenes = [SceneItem(**item) for item in raw_json]
                        job.scene_count = len(job.scenes)
                        job.scene_plan = existing_txt_path.read_text(encoding="utf-8") if existing_txt_path.exists() else ""
                        job.scene_plan_source = "reused_existing"
                        loaded_existing_plan = True
                        log(f"[1. Aşama] Mevcut senaryo planı korundu ({len(job.scenes)} sahne). Tekrar prompt gönderilmedi, mevcut senaryo ve görsellere göre devam ediliyor.")
                    else:
                        log("[1. Aşama] Klasördeki senaryo taslağı ham brief veya geçersiz içerik barındırdığı için güncel senaryo ayrıştırması başlatılıyor…")
            except Exception as e:
                log(f"Mevcut senaryo planı okunamadı: {e}")

        if not loaded_existing_plan:
            user_script = (job.scenario or "").strip()
            if not user_script and existing_txt_path.exists():
                try:
                    user_script = existing_txt_path.read_text(encoding="utf-8").strip()
                except Exception:
                    pass

            model_text = None
            if job.reference_images and hasattr(provider, "attach_references"):
                ref_label = "karakter/masal referans görseli" if (project_name == PROJECT_IKIZLER or is_animation_content_type(job)) else "referans görseli"
                log(f"[1. Aşama] Senaryo planlaması için {len(job.reference_images)} {ref_label} ekleniyor…")
                try:
                    provider.attach_references(job.reference_images)
                except Exception as ref_err:
                    log(f"[1. Aşama] Referans görsel ekleme uyarısı: {ref_err}")

            # Ekrandaki mevcut Gemini oturumunda zaten üretilmiş geçerli bir senaryo var mı kontrol et
            if hasattr(provider, "read_response_text"):
                try:
                    existing_screen = provider.read_response_text(latest_only=True)
                    existing_clean = clean_model_response_text(existing_screen) if existing_screen else ""
                    if existing_clean and has_scene_markers(existing_clean):
                        log(f"[1. Aşama] Gemini ekranında üretilmiş senaryo tespit edildi ({len(existing_clean)} karakter). Doğrudan alınıyor.")
                        model_text = existing_screen
                except Exception as ex:
                    log(f"[1. Aşama] Ekrandan senaryo ön okuma notu: {ex}")

            max_script_retries = 5
            script_attempt = 0
            while script_attempt < max_script_retries and not model_text:
                script_attempt += 1
                try:
                    if hasattr(provider, "is_aborted") and provider.is_aborted:
                        raise InterruptedError("İşlem kullanıcı tarafından durduruldu.")
                    breakdown_prompt = build_script_breakdown_prompt(job, retry_attempt=script_attempt)
                    log(f"Senaryo planlama prompt'u Gemini'ye gönderiliyor (Deneme {script_attempt}/{max_script_retries})…")
                    provider.send_prompt(breakdown_prompt)
                    time.sleep(1)
                    if hasattr(provider, "wait_for_new_response_text"):
                        log("Gemini'nin senaryo yanıtı bekleniyor (maks. 30 sn)…")
                        try:
                            model_text = provider.wait_for_new_response_text(
                                timeout_seconds=30,
                                min_scenes=job.scene_count,
                            )
                        except TypeError:
                            model_text = provider.wait_for_new_response_text(timeout_seconds=30)
                    else:
                        model_text = provider.read_response_text()

                    # wait_for_new_response_text zaman aşımına uğrasa bile ekrandaki metni doğrudan kontrol et
                    if not model_text and hasattr(provider, "read_response_text"):
                        fallback_screen = provider.read_response_text(latest_only=True)
                        if fallback_screen and has_scene_markers(clean_model_response_text(fallback_screen)):
                            log("[1. Aşama] Senaryo metni doğrudan Gemini ekranından okundu.")
                            model_text = fallback_screen

                    if model_text and hasattr(provider, "check_text_safety_or_limits"):
                        provider.check_text_safety_or_limits(model_text)
                    if model_text:
                        parsed_test = parse_scene_breakdown(model_text, default_count=job.scene_count)
                        if len(parsed_test) < job.scene_count and hasattr(provider, "read_response_text"):
                            time.sleep(1)
                            latest_txt = provider.read_response_text(latest_only=True) or ""
                            if len(latest_txt) > len(model_text):
                                model_text = latest_txt
                        break
                    else:
                        log(f"Gemini'den metin yanıtı henüz gelmedi. Anında tekrar deneniyor (Deneme {script_attempt}/{max_script_retries})…")
                        if script_attempt < max_script_retries:
                            time.sleep(1)
                except (RateLimitError, InterruptedError):
                    raise
                except SafetyRefusalError as sre:
                    model_text = None
                    if script_attempt < max_script_retries:
                        log(
                            f"⚠️ [1. Aşama] Gemini metin güvenlik uyarısı verdi ({sre}). "
                            f"Temiz bir sohbet oturumunda yalın senaryo talimatıyla anında tekrarlanıyor (Deneme {script_attempt}/{max_script_retries})…"
                        )
                        try:
                            if hasattr(provider, "reset_chat_session"):
                                provider.reset_chat_session()
                            if job.reference_images and hasattr(provider, "attach_references"):
                                provider.attach_references(job.reference_images)
                        except Exception as reset_err:
                            log(f"[1. Aşama] Oturum yenileme notu: {reset_err}")
                        time.sleep(1)
                        continue
                    log(f"⚠️ [1. Aşama] Gemini metin güvenlik uyarısı verdi ({sre}).")
                    break
                except Exception as e:
                    if script_attempt < max_script_retries:
                        log(f"⏳ [1. Aşama] Gemini senaryo üretim uyarısı (limit hatası değil): {e}")
                        log(f"🔄 [1. Aşama] Anında tekrar deneniyor (Deneme {script_attempt}/{max_script_retries})…")
                        time.sleep(2)
                    else:
                        log(f"Bilgi: Gemini metin ayrıştırma notu: {e}")
                        break

            # Tüm denemelerden sonra model_text hala boşsa ekrandaki son metni bir kez daha kontrol et
            if not model_text and hasattr(provider, "read_response_text"):
                fallback_screen = provider.read_response_text(latest_only=True)
                if fallback_screen and has_scene_markers(clean_model_response_text(fallback_screen)):
                    log("[1. Aşama] Senaryo planı doğrudan Gemini ekranından doğrulandı ve alındı.")
                    model_text = fallback_screen

            # Gemini'nin yanıtı arayüz kalıntılarından arındırılır (içerik değiştirilmez)
            gemini_text = clean_model_response_text(model_text) if model_text else ""

            gemini_plan_ok = bool(gemini_text) and has_scene_markers(gemini_text)
            if model_text and not gemini_plan_ok:
                log(
                    "⚠️ [1. Aşama] Gemini yanıtında [SAHNE N] / BAŞLIK / GÖRSEL_PROMPT blokları bulunamadı; "
                    f"yanıtın ilk kısmı: {gemini_text[:200]!r}"
                )

            if gemini_plan_ok:
                # Gemini'den gelen gerçek senaryo doğrudan kullanılır
                job.scenes = parse_scene_breakdown(
                    gemini_text,
                    default_count=job.scene_count,
                    default_seconds=job.seconds_per_scene,
                    fallback_prompt="",
                    is_animation=is_animation_content_type(job),
                    job=job,
                )
                job.scene_plan = gemini_text
                job.scene_plan_source = "gemini_generated"
            elif user_script:
                # Kullanıcının hazır senaryosu varsa o kullanılır
                job.scenes = parse_scene_breakdown(
                    user_script,
                    default_count=job.scene_count,
                    default_seconds=job.seconds_per_scene,
                    fallback_prompt="",
                    is_animation=is_animation_content_type(job),
                    job=job,
                )
                job.scene_plan = user_script
                job.scene_plan_source = "user_provided"
                log("[1. Aşama] Kullanıcının girdiği hazır senaryo metni sahne planına dönüştürüldü.")
            else:
                # Ne Gemini senaryosu ne de hazır senaryo yoksa ASLA sahte şablon yazma; açıkça bildir
                raise RuntimeError(
                    "Gemini'den geçerli bir senaryo planı alınamadı ([SAHNE N] blokları eksik). "
                    "Lütfen brief metnini kontrol edip 'Yeniden Başlat' ile tekrar deneyin veya 'Hazır Senaryo' kutusuna sahne metnini girin."
                )

            (dirs["scenarios"] / "tam_senaryo.txt").write_text(job.scene_plan, encoding="utf-8")
            (dirs["scenarios"] / "sahne_plani.json").write_text(
                json.dumps([s.to_dict() for s in job.scenes], ensure_ascii=False, indent=2), encoding="utf-8"
            )
            log(f"[1. Aşama] Senaryo ve sahne planı klasöre kaydedildi ({len(job.scenes)} sahne, kaynak: {job.scene_plan_source}): sahne_plani.json, tam_senaryo.txt")
        report.add(
            StageResult(
                "script_breakdown",
                StageStatus.SUCCESS,
                f"{len(job.scenes)} sahne başarıyla ayrıştırıldı ve planlandı.",
                {"scene_count": len(job.scenes)},
            )
        )
        if job_queue:
            try:
                job_queue.update_progress(job.job_id, job.scenes, "script_breakdown", output_dir=job.output_dir)
            except Exception:
                pass
        log(f"1. Aşama Tamamlandı: {len(job.scenes)} sahne hazırlandı. Otomatik olarak 2. Aşamaya (Görseller) geçiliyor…")

        # ------------------------------------------------------------------
        # CANLI MOD: 2. AŞAMA - Sırayla Sahne Görsellerinin Üretimi (Tam Otomatik)
        # ------------------------------------------------------------------
        log("=== [2. AŞAMA] Sırayla Sahne Görselleri Üretiliyor ===")
        old_images = provider.snapshot_images() if hasattr(provider, "snapshot_images") else set()
        target_imgs = cfg.get("target_image_scenes")
        for sc in job.scenes:
            target_img = dirs["images"] / f"sahne_{sc.index}.png"
            if target_imgs is not None and sc.index not in target_imgs:
                if target_img.exists() and target_img.stat().st_size > 20000:
                    sc.image_path = str(target_img)
                    sc.status = "image_done"
                    log(f"[Sahne {sc.index}] Görsel korundu (seçilmedi): {target_img.name}")
                continue

            if target_img.exists() and target_img.stat().st_size > 20000:
                sc.image_path = str(target_img)
                sc.status = "image_done"
                log(f"[Sahne {sc.index}] Mevcut Gemini üretimi görsel korundu: {target_img.name} ({target_img.stat().st_size / 1024:.1f} KB)")
                continue

            # Müşterinin ürün veya karakter referans görseli varsa her sahne görseli çiziminde Gemini'ye iliştir
            if job.reference_images and hasattr(provider, "attach_references"):
                ref_label = "Karakter/masal referans görseli" if (job.project_name == PROJECT_IKIZLER or is_animation_content_type(job)) else "Orijinal ürün referans görseli"
                log(f"[Sahne {sc.index}] {ref_label} ({len(job.reference_images)} adet) ekleniyor…")
                try:
                    provider.attach_references(job.reference_images)
                except Exception as ref_err:
                    log(f"[Sahne {sc.index}] Referans görsel ekleme uyarısı: {ref_err}")

            log(f"[Sahne {sc.index}/{len(job.scenes)}] Görsel üretimi isteniyor: {sc.title}")
            img_prompt = build_scene_image_prompt(sc, job)
            max_img_retries = 8
            img_attempt = 0
            while img_attempt <= max_img_retries and sc.status != "image_done":
                try:
                    init_count = provider.get_image_count() if hasattr(provider, "get_image_count") else 0
                    if hasattr(provider, "snapshot_images"):
                        try:
                            old_images.update(provider.snapshot_images())
                        except Exception:
                            pass
                    provider.send_prompt(img_prompt)
                    new_img = None
                    if hasattr(provider, "wait_for_new_image"):
                        new_img = provider.wait_for_new_image(timeout_minutes=1, old_sources=old_images, initial_count=init_count)
                    saved = False
                    if hasattr(provider, "save_latest_generated_image"):
                        saved = provider.save_latest_generated_image(target_img)
                    elif new_img and hasattr(provider, "download_file"):
                        saved = provider.download_file(new_img, target_img)

                    if saved:
                        sc.image_path = str(target_img)
                        if new_img:
                            old_images.add(new_img)
                        if hasattr(provider, "snapshot_images"):
                            try:
                                old_images.update(provider.snapshot_images())
                            except Exception:
                                pass
                        sc.status = "image_done"
                        log(f"[Sahne {sc.index}] Gemini üretimi görsel kaydedildi: {target_img.name}")
                        break
                    else:
                        img_attempt += 1
                        if img_attempt <= max_img_retries:
                            log(f"⏳ [Sahne {sc.index}] Gemini yeni görsel üretemedi. Anında tekrar deneniyor (Deneme {img_attempt}/{max_img_retries})…")
                            time.sleep(2)
                        else:
                            sc.status = "image_failed"
                            log(f"[Sahne {sc.index}] UYARI: Gemini yeni görsel üretemedi veya yakalanamadı.")
                except RateLimitError as rle:
                    log(f"GEMINI LİMİTİNE TAKILDI (Görsel): {rle}")
                    sc.status = "image_failed"
                    raise rle
                except (InterruptedError, KeyboardInterrupt):
                    raise
                except Exception as e:
                    img_attempt += 1
                    if img_attempt <= max_img_retries:
                        log(f"⏳ [Sahne {sc.index}] Gemini görsel üretim uyarısı (limit hatası değil): {e}")
                        log(f"🔄 [Sahne {sc.index}] Anında tekrar deneniyor (Deneme {img_attempt}/{max_img_retries})…")
                        time.sleep(2)
                        if job.reference_images and hasattr(provider, "attach_references"):
                            try:
                                provider.attach_references(job.reference_images)
                            except Exception:
                                pass
                        err_str = str(e).lower()
                        if any(q in err_str for q in ["something else", "try something", "başka bir şey", "deneyebilir miyim"]):
                            log(f"💬 [Sahne {sc.index}] Gemini'nin sorusuna ('Can I try something else for you?') olumlu yanıt ile görsel üretim talimatı gönderiliyor…")
                            img_prompt = f"Yes, please generate the 9:16 image for this scene now: {build_scene_image_prompt(sc, job)}"
                        elif isinstance(e, SafetyRefusalError) or any(w in err_str for w in ["programlanmadım", "dil modeli", "metin oluşturuyorum"]):
                            log(f"⚠️ [Sahne {sc.index}] Gemini görsel güvenlik/politika uyarısı verdi. Prompt kurallara uygun şekilde sadeleştiriliyor…")
                            clean_topic = sanitize_topic_for_prompts(sc.image_prompt)
                            if is_animation_content_type(job):
                                sc.image_prompt = f"3D cartoon animation Pixar style: Adorable stylized 3D scene of {clean_topic or 'characters'}, vibrant colors, soft studio lighting, 9:16 vertical orientation"
                            else:
                                sc.image_prompt = f"Commercial product photography: Beautiful aesthetic presentation of {clean_topic or 'the product'}, elegant minimalist studio setting, soft daylight, 8k resolution, 9:16 vertical orientation"
                            img_prompt = build_scene_image_prompt(sc, job)
                            try:
                                (dirs["scenarios"] / "sahne_plani.json").write_text(
                                    json.dumps([s.to_dict() for s in job.scenes], ensure_ascii=False, indent=2), encoding="utf-8"
                                )
                            except Exception:
                                pass
                    else:
                        log(f"Sahne {sc.index} görsel uyarısı: {e}")
                        sc.status = "image_failed"

        report.add(
            StageResult(
                "scene_images",
                StageStatus.SUCCESS,
                f"{len(job.scenes)} sahne görseli hazırlandı.",
            )
        )
        if job_queue:
            try:
                job_queue.update_progress(job.job_id, job.scenes, "scene_images", output_dir=job.output_dir)
            except Exception:
                pass
        log("2. Aşama Tamamlandı: Görseller hazır. Otomatik olarak 3. Aşamaya (Video Üretimi) geçiliyor…")

        # ------------------------------------------------------------------
        # CANLI MOD: 3. AŞAMA - Sırayla Sahne Videolarının Üretimi (Tam Otomatik)
        # ------------------------------------------------------------------
        log("=== [3. AŞAMA] Sırayla Sahne Videoları Üretiliyor ===")
        timeout_minutes = int(cfg.get("video_timeout_minutes") or 3)
        completed_videos = 0
        last_video_src = None
        target_vids = cfg.get("target_video_scenes")

        for sc in job.scenes:
            target_vid = dirs["videos"] / f"sahne_{sc.index}.mp4"
            if target_vids is not None and sc.index not in target_vids:
                if target_vid.exists() and target_vid.stat().st_size > 100000:
                    sc.video_path = str(target_vid)
                    sc.status = "video_done"
                    completed_videos += 1
                    report.video_src_detected = True
                    last_video_src = str(target_vid)
                    log(f"[Sahne {sc.index}] Video korundu (seçilmedi): {target_vid.name}")
                continue

            if target_vid.exists() and target_vid.stat().st_size > 100000:
                sc.video_path = str(target_vid)
                sc.status = "video_done"
                completed_videos += 1
                report.video_src_detected = True
                last_video_src = str(target_vid)
                log(f"[Sahne {sc.index}] Mevcut video korundu: {target_vid.name} ({target_vid.stat().st_size / 1024 / 1024:.2f} MB)")
                continue

            # Üretilen sahne görseli varsa video üretimi için Gemini'ye ek olarak yükle
            scene_img = Path(sc.image_path) if sc.image_path else (dirs["images"] / f"sahne_{sc.index}.png")
            has_scene_image = scene_img.exists() and scene_img.stat().st_size > 0
            if has_scene_image:
                log(f"[Sahne {sc.index}] Üretilen sahne görseli video üretimi için ekleniyor: {scene_img.name}")
                try:
                    if hasattr(provider, "attach_references"):
                        provider.attach_references([str(scene_img)])
                except Exception as img_err:
                    log(f"[Sahne {sc.index}] Uyarı: Sahne görseli eklenemedi, metin promptuyla devam ediliyor: {img_err}")

            log(f"[Sahne {sc.index}/{len(job.scenes)}] Video üretimi başlatılıyor: {sc.title}")
            vid_prompt = build_scene_video_prompt(sc, job, has_image=has_scene_image)
            max_policy_retries = 2
            policy_attempt = 0
            traffic_attempt = 0
            max_non_limit_retries = 5
            non_limit_attempt = 0
            video_done = False

            while policy_attempt <= max_policy_retries and not video_done:
                try:
                    provider.send_prompt(vid_prompt)
                    log(f"[Sahne {sc.index}] Yeni video bekleniyor…")
                    video_src = provider.wait_for_new_video(timeout_minutes, old_sources)
                    if not is_new_http_video(video_src, old_sources):
                        raise TimeoutError(f"Eski video yeni kabul edilmedi: {video_src}")
                    old_sources.add(video_src)
                    last_video_src = video_src
                    report.video_src_detected = True

                    if hasattr(provider, "download_file"):
                        provider.download_file(video_src, target_vid)
                        sc.video_path = str(target_vid)
                    sc.status = "video_done"
                    completed_videos += 1
                    video_done = True
                    log(f"[Sahne {sc.index}] Video başarıyla kaydedildi: {target_vid.name}")
                    if job_queue:
                        try:
                            job_queue.update_progress(job.job_id, job.scenes, f"scene_video_{sc.index}", output_dir=job.output_dir)
                        except Exception:
                            pass
                except HighTrafficCapacityError as hte:
                    traffic_attempt += 1
                    wait_sec = 5
                    log(f"⏳ [Sahne {sc.index}] Gemini sunucu yoğunluğu tespit edildi: {hte}")
                    log(f"🔄 [Sahne {sc.index}] {wait_sec} saniye sonra anında tekrar deneniyor (Yoğunluk denemesi {traffic_attempt}, video üretilene kadar devam edilecek)…")
                    time.sleep(wait_sec)
                    if has_scene_image and hasattr(provider, "attach_references"):
                        try:
                            provider.attach_references([str(scene_img)])
                        except Exception as ref_e:
                            log(f"[Sahne {sc.index}] Referans görsel ekleme uyarısı: {ref_e}")
                    continue
                except TransientGeminiError as tge:
                    non_limit_attempt += 1
                    wait_sec = 2
                    log(f"⏳ [Sahne {sc.index}] Gemini geçici hata uyarısı tespit edildi (limit hatası değil): {tge}")
                    if non_limit_attempt <= max_non_limit_retries:
                        log(f"🔄 [Sahne {sc.index}] Anında tekrar deneniyor (Deneme {non_limit_attempt}/{max_non_limit_retries})…")
                        time.sleep(wait_sec)
                        # Üst üste hata durumunda sohbeti temizle
                        if non_limit_attempt >= 2 and hasattr(provider, "reset_chat_session"):
                            try:
                                log(f"🔄 [Sahne {sc.index}] Gemini oturumu temizlenip taze sohbet başlatılıyor…")
                                provider.reset_chat_session()
                                old_sources.update(provider.snapshot_videos())
                            except Exception as reset_e:
                                log(f"[Sahne {sc.index}] Oturum sıfırlama uyarısı: {reset_e}")
                        # Doğrudan metin video üretim promptuyla dene
                        vid_prompt = build_scene_video_prompt(sc, job, has_image=False)
                        log(f"🎬 [Sahne {sc.index}] Doğrudan metin video üretim promptuyla deneniyor: {vid_prompt[:120]}…")
                        continue
                    else:
                        log(f"HATA: [Sahne {sc.index}] {max_non_limit_retries} kez denemeye rağmen hata devam etti: {tge}")
                        report.add(
                            StageResult(
                                "scene_videos",
                                StageStatus.FAILED,
                                str(tge),
                                {"requested_scenes": job.scene_count, "completed": completed_videos, "failed_scene": sc.index},
                            )
                        )
                        report.add(
                            StageResult(
                                "verify_outputs",
                                StageStatus.FAILED,
                                "Video üretimi geçici hata tekrarı nedeniyle tamamlanamadı.",
                            )
                        )
                        report.delivered = False
                        report.finalize()
                        save_job(job, report)
                        _log_report(log, report)
                        return report
                except RateLimitError as rle:
                    log(f"GEMINI LİMİTİNE TAKILDI: {rle}")
                    sc.status = "rate_limited"
                    report.add(
                        StageResult(
                            "scene_videos",
                            StageStatus.RATE_LIMITED,
                            f"Gemini video limitine ulaşıldı. {completed_videos}/{len(job.scenes)} video tamamlandı. Limit sıfırlandığında sistem Sahne {sc.index}'den devam edebilir.",
                            {"requested_scenes": job.scene_count, "completed": completed_videos, "pending_scene": sc.index},
                        )
                    )
                    report.delivered = False
                    report.finalize()
                    save_job(job, report)
                    if job_queue:
                        try:
                            job_queue.update_progress(job.job_id, job.scenes, f"rate_limited_scene_{sc.index}", output_dir=job.output_dir)
                        except Exception:
                            pass
                    _log_report(log, report)
                    return report
                except (VideoPolicyRefusalError, SafetyRefusalError) as pref:
                    policy_attempt += 1
                    refusal_msg = str(pref)
                    log(f"⚠️ [Sahne {sc.index}] Gemini video politika uyarısı tespit edildi: {refusal_msg}")
                    if policy_attempt <= max_policy_retries:
                        log(
                            f"🔄 [Sahne {sc.index}] Senaryo bütünlüğü korunarak prompt Gemini kurallarına uygun şekilde revize ediliyor "
                            f"(Revizyon {policy_attempt}/{max_policy_retries})…"
                        )
                        vid_prompt, revised_desc = revise_scene_video_prompt_for_safety(
                            sc,
                            job,
                            refusal_reason=refusal_msg,
                            attempt=policy_attempt,
                            has_image=has_scene_image,
                        )
                        # Revize edilen sahne planını dosyalara ve arayüze hemen yansıt
                        try:
                            (dirs["scenarios"] / "sahne_plani.json").write_text(
                                json.dumps([s.to_dict() for s in job.scenes], ensure_ascii=False, indent=2),
                                encoding="utf-8",
                            )
                            updated_text = "\n\n".join(
                                f"[SAHNE {s.index}]\nBAŞLIK: {s.title}\nAÇIKLAMA: {s.description}\n"
                                f"GÖRSEL_PROMPT: {s.image_prompt}\nVIDEO_PROMPT: {s.video_prompt}\n"
                                f"SESLENDIRME: {s.voiceover_text}"
                                for s in job.scenes
                            )
                            job.scene_plan = updated_text
                            (dirs["scenarios"] / "tam_senaryo.txt").write_text(updated_text, encoding="utf-8")
                        except Exception as file_err:
                            log(f"[Sahne {sc.index}] Senaryo dosyası güncelleme uyarısı: {file_err}")

                        log(f"📝 [Sahne {sc.index}] Düzenlenen Yeni Prompt: {vid_prompt[:130]}…")
                        log(f"🚀 [Sahne {sc.index}] Düzenlenen prompt Gemini'ye tekrar gönderiliyor…")
                        time.sleep(1)
                        continue
                    else:
                        log(f"HATA: [Sahne {sc.index}] {max_policy_retries} kez politika revizyonuna rağmen video üretilemedi: {pref}")
                        report.add(
                            StageResult(
                                "scene_videos",
                                StageStatus.FAILED,
                                str(pref),
                                {"requested_scenes": job.scene_count, "completed": completed_videos, "refusal_scene": sc.index},
                            )
                        )
                        report.add(
                            StageResult(
                                "verify_outputs",
                                StageStatus.FAILED,
                                "Video üretimi politika reddi nedeniyle tamamlanamadı.",
                            )
                        )
                        report.delivered = False
                        report.finalize()
                        save_job(job, report)
                        _log_report(log, report)
                        return report
                except (InterruptedError, KeyboardInterrupt):
                    raise
                except Exception as exc:
                    if isinstance(exc, RateLimitError):
                        log(f"GEMINI LİMİTİNE TAKILDI: {exc}")
                        sc.status = "rate_limited"
                        report.add(
                            StageResult(
                                "scene_videos",
                                StageStatus.RATE_LIMITED,
                                f"Gemini video limitine ulaşıldı. {completed_videos}/{len(job.scenes)} video tamamlandı.",
                            )
                        )
                        report.delivered = False
                        report.finalize()
                        save_job(job, report)
                        _log_report(log, report)
                        return report

                    non_limit_attempt += 1
                    if non_limit_attempt <= max_non_limit_retries:
                        wait_sec = 2
                        log(f"⏳ [Sahne {sc.index}] Gemini hata uyarısı verdi (limit hatası değil): {exc}")
                        log(f"🔄 [Sahne {sc.index}] Anında tekrar deneniyor (Deneme {non_limit_attempt}/{max_non_limit_retries})…")
                        time.sleep(wait_sec)
                        if has_scene_image and hasattr(provider, "attach_references"):
                            try:
                                provider.attach_references([str(scene_img)])
                            except Exception as ref_e:
                                log(f"[Sahne {sc.index}] Referans görsel ekleme uyarısı: {ref_e}")
                        continue

                    log(f"HATA: Video üretimi başarısız: {exc}")
                    report.add(
                        StageResult(
                            "scene_videos",
                            StageStatus.FAILED,
                            str(exc),
                            {"requested_scenes": job.scene_count, "completed": completed_videos},
                        )
                    )
                    report.add(
                        StageResult(
                            "verify_outputs",
                            StageStatus.FAILED,
                            "Video üretimi tamamlanamadığı için teslimat başarısız sayıldı.",
                        )
                    )
                    report.delivered = False
                    report.finalize()
                    save_job(job, report)
                    _log_report(log, report)
                    return report


        report.add(
            StageResult(
                "scene_videos",
                StageStatus.SUCCESS,
                f"{completed_videos}/{len(job.scenes)} sahne videosu başarıyla üretildi.",
                {"completed": completed_videos},
            )
        )
        if job_queue:
            try:
                job_queue.update_progress(job.job_id, job.scenes, "scene_videos", output_dir=job.output_dir)
            except Exception:
                pass

        # ------------------------------------------------------------------
        # Sahne Videolarını Tek Bir Videoda Birleştirme (FFmpeg - tam_video.mp4)
        # ------------------------------------------------------------------
        valid_video_paths = [
            sc.video_path for sc in job.scenes
            if sc.video_path and Path(sc.video_path).is_file() and Path(sc.video_path).stat().st_size > 1000
        ]
        merged_video_path = dirs["videos"] / "tam_video.mp4"
        if valid_video_paths:
            log(f"=== Tüm Sahne Videoları ({len(valid_video_paths)} sahne) FFmpeg ile 'tam_video.mp4' Olarak Birleştiriliyor ===")
            try:
                from services.video_composer import merge_scene_videos

                composed = merge_scene_videos(valid_video_paths, merged_video_path, log)
                if composed and composed.exists() and composed.stat().st_size > 1000:
                    log(f"Tam video hazır: {composed.name} ({composed.stat().st_size / 1024 / 1024:.2f} MB)")
                else:
                    log("UYARI: Video birleştirme çıktısı alınamadı; münferit sahne videoları korunuyor.")
            except Exception as comp_err:
                log(f"UYARI: Video birleştirme sırasında hata: {comp_err}")

        # Instagram paylaşım paketi (Başlık, Açıklama, 5 Hashtag) ve Kapak Fotoğrafı üretimi
        try:
            is_selective = bool(job.target_video_scenes or job.target_image_scenes)
            use_provider = provider if (not is_selective and type(provider).__name__ != "FakeProvider") else None
            prepare_instagram_package(job, dirs, log, provider=use_provider)
        except Exception as insta_err:
            log(f"Instagram paylaşım paketi hazırlama uyarısı: {insta_err}")

        # Güncel sahne planını diske kaydet
        try:
            (dirs["scenarios"] / "sahne_plani.json").write_text(
                json.dumps([s.to_dict() for s in job.scenes], ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            log(f"sahne_plani.json güncellenirken uyarı: {e}")

        # ------------------------------------------------------------------
        # CANLI MOD: 4. AŞAMA - Arşivleme ve Drive Teslimatı (Tam Otomatik)
        # ------------------------------------------------------------------
        log("=== [4. AŞAMA] Arşivleme ve Teslimat ===")
        upload_verified = False
        if job.target_folder_id:
            log(f"Google Drive klasör teslimatı başlatılıyor (Ana Klasör: {job.target_folder_id})…")
            try:
                if hasattr(provider, "upload_all_outputs_to_drive"):
                    upload_res = provider.upload_all_outputs_to_drive(dirs)
                    upload_verified = bool(upload_res.get("verified"))
                    campaign_folder = upload_res.get("folder") or dirs["base"].name
                    folders = upload_res.get("folders") or ["01_Senaryo", "02_Gorseller", "03_Videolar"]
                    if upload_verified:
                        report.add(
                            StageResult(
                                "drive_upload",
                                StageStatus.SUCCESS,
                                f"Drive klasör aktarımı doğrulandı: {campaign_folder} ({', '.join(folders)}, tam_video.mp4 dahil)",
                                {"campaign_folder": campaign_folder, "folders": folders},
                            )
                        )
                    else:
                        report.add(
                            StageResult(
                                "drive_upload",
                                StageStatus.PENDING,
                                f"Drive yüklemesi başlatıldı ancak teyit bekleniyor: {campaign_folder}",
                                {"campaign_folder": campaign_folder, "folders": folders},
                            )
                        )
                else:
                    target_upload_src = merged_video_path if merged_video_path.exists() else last_video_src
                    upload_res = provider.upload_to_drive(target_upload_src)
                    upload_verified = bool(upload_res.get("verified"))
                    fn = upload_res.get("filename") or ""
                    if upload_verified:
                        report.add(StageResult("drive_upload", StageStatus.SUCCESS, f"Drive yüklemesi doğrulandı: {fn}"))
                    else:
                        report.add(StageResult("drive_upload", StageStatus.PENDING, f"Yükleme başlatıldı ancak doğrulanamadı: {fn}"))
            except Exception as exc:
                log(f"Drive teslimat hatası: {exc}")
                report.add(StageResult("drive_upload", StageStatus.FAILED, f"Drive aktarımı başarısız: {exc}"))
                report.add(StageResult("verify_outputs", StageStatus.FAILED, "Drive aktarımı başarısız olduğu için teslimat onaylanmadı."))
                report.delivered = False
                report.finalize()
                save_job(job, report)
                _log_report(log, report)
                return report
        else:
            log("Hedef Drive klasör ID tanımlı değil; yerel arşivleme geçerli kabul edildi.")

        if upload_verified or (not job.target_folder_id and completed_videos > 0):
            report.delivered = True
            report.add(StageResult("verify_outputs", StageStatus.SUCCESS, f"Çıktılar doğrulandı ve teslim edildi: {dirs['base'].name}"))
            try:
                (dirs["base"] / ".delivered").write_text(
                    json.dumps({
                        "job_id": job.job_id,
                        "delivered_at": datetime.now(timezone.utc).isoformat(),
                        "brand_name": job.brand_name,
                    }, ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
            except Exception:
                pass
        else:
            report.delivered = False
            msg = "Drive klasör teslimatı doğrulanamadı; başarılı teslim bildirilmedi." if job.target_folder_id else "Üretilen video çıktısı doğrulanamadı."
            report.add(StageResult("verify_outputs", StageStatus.FAILED, msg))

    except InterruptedError:
        log("🛑 İşlem kullanıcı isteğiyle durduruldu.")
        report.overall_status = "interrupted"
        report.delivered = False
        report.finalize()
        save_job(job, report)
        _log_report(log, report)
        return report

    finally:
        if owns_provider and provider is not None:
            try:
                provider.close()
            except Exception:
                pass

    report.finalize()
    save_job(job, report)
    _log_report(log, report)
    return report


def _log_report(log: Callable[[str], None], report: WorkflowReport) -> None:
    log("—— Üretim Raporu ——")
    for line in report.summary_lines():
        log(line)
