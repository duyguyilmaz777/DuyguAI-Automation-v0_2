import json
import tempfile
import unittest
from pathlib import Path

from models.job import StageStatus
from providers.video_sources import is_new_http_video
from services.logging_utils import sanitize_log_line
from services.prompt_builder import build_production_prompt
from services.workflow import create_job_order, run_content_workflow
from tests.fakes import FakeProvider

CFG = {
    "gem_url": "https://gemini.google.com/gem/test",
    "target_folder_id": "folder-id",
    "content_type": "ürün reklamı",
    "video_format": "9:16",
    "scene_count": 1,
    "seconds_per_scene": 5,
    "dry_run": True,
    "video_timeout_minutes": 1,
    "brief": "Kırmızı shaker için reels",
    "scenario": "",
    "extra_notes": "",
}


def collect():
    lines = []
    return lines, lines.append


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        import services.workflow as wf
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._old_outputs = wf.OUTPUTS_DIR
        self._old_jobs = wf.JOBS_DIR
        wf.OUTPUTS_DIR = Path(self._tmp_dir.name) / "outputs"
        wf.JOBS_DIR = Path(self._tmp_dir.name) / "jobs"
        wf.OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
        wf.JOBS_DIR.mkdir(parents=True, exist_ok=True)

        from unittest.mock import patch
        self._sleep_patcher = patch("time.sleep", return_value=None)
        self._sleep_patcher.start()

    def tearDown(self):
        import services.workflow as wf
        wf.OUTPUTS_DIR = self._old_outputs
        wf.JOBS_DIR = self._old_jobs
        try:
            self._sleep_patcher.stop()
        except Exception:
            pass
        try:
            self._tmp_dir.cleanup()
        except Exception:
            pass

    def test_brief_plus_refs_dry_run_does_not_send_or_upload(self):
        lines, log = collect()
        provider = FakeProvider()
        cfg = dict(CFG)
        report = run_content_workflow(cfg, ["ref.png"], log, provider=provider)
        self.assertEqual(report.overall_status, "dry_run")
        self.assertFalse(report.delivered)
        self.assertNotIn("send_prompt", provider.calls)
        self.assertNotIn("upload_to_drive", provider.calls)
        self.assertIn("connect", provider.calls)
        self.assertTrue(any("Üretim isteği Gemini'ye gönderilmedi" in x for x in lines))

    def test_scenario_only_stores_scene_plan(self):
        cfg = dict(CFG)
        cfg["brief"] = ""
        cfg["scenario"] = "Sahne 1: kapı açılır\nSahne 2: ürün döner"
        job = create_job_order(cfg, [])
        self.assertEqual(job.scene_plan_source, "user_provided")
        self.assertIn("kapı açılır", job.scene_plan)
        prompt = build_production_prompt(job)
        self.assertIn("kapı açılır", prompt)
        self.assertNotIn("Create a polished product video", prompt)

    def test_no_refs_still_builds_job(self):
        job = create_job_order(CFG, [])
        self.assertEqual(job.reference_images, [])
        self.assertIn("referans görsel yok", job.generated_prompt)

    def test_live_connection_error_is_failure(self):
        cfg = dict(CFG)
        cfg["dry_run"] = False
        provider = FakeProvider(connect_error=RuntimeError("CDP timeout"))
        lines, log = collect()
        report = run_content_workflow(cfg, [], log, provider=provider)
        self.assertEqual(report.overall_status, "failed")
        self.assertFalse(report.delivered)
        self.assertNotIn("send_prompt", provider.calls)
        self.assertNotIn("upload_to_drive", provider.calls)
        connect = next(s for s in report.stages if s.name == "gemini_connect")
        self.assertEqual(connect.status, StageStatus.FAILED)

    def test_video_timeout_does_not_upload_or_claim_success(self):
        cfg = dict(CFG)
        cfg["dry_run"] = False
        provider = FakeProvider(video_error=TimeoutError("zaman aşımı"))
        lines, log = collect()
        report = run_content_workflow(cfg, [], log, provider=provider)
        self.assertEqual(report.overall_status, "failed")
        self.assertFalse(report.delivered)
        self.assertIn("send_prompt", provider.calls)
        self.assertNotIn("upload_to_drive", provider.calls)
        video_stage = next(s for s in report.stages if s.name == "scene_videos")
        self.assertEqual(video_stage.status, StageStatus.FAILED)

    def test_old_video_is_not_accepted_as_new(self):
        old = "https://example.invalid/old-video.mp4"
        self.assertFalse(is_new_http_video(old, {old}))
        self.assertTrue(is_new_http_video("https://example.invalid/new.mp4", {old}))
        self.assertFalse(is_new_http_video("blob:abc", set()))

        cfg = dict(CFG)
        cfg["dry_run"] = False
        provider = FakeProvider(new_video=old, old_videos={old})
        lines, log = collect()
        report = run_content_workflow(cfg, [], log, provider=provider)
        self.assertEqual(report.overall_status, "failed")
        self.assertNotIn("upload_to_drive", provider.calls)
        self.assertEqual(provider.old_sources_seen, {old})

    def test_unverified_drive_is_not_successful_delivery(self):
        cfg = dict(CFG)
        cfg["dry_run"] = False
        provider = FakeProvider(upload_verified=False)
        lines, log = collect()
        report = run_content_workflow(cfg, [], log, provider=provider)
        self.assertFalse(report.delivered)
        self.assertEqual(report.overall_status, "failed")
        verify = next(s for s in report.stages if s.name == "verify_outputs")
        self.assertEqual(verify.status, StageStatus.FAILED)

    def test_drive_error_is_not_success(self):
        cfg = dict(CFG)
        cfg["dry_run"] = False
        provider = FakeProvider(upload_error=RuntimeError("Drive UI bulunamadı"))
        lines, log = collect()
        report = run_content_workflow(cfg, [], log, provider=provider)
        self.assertFalse(report.delivered)
        self.assertEqual(report.overall_status, "failed")
        upload = next(s for s in report.stages if s.name == "drive_upload")
        self.assertEqual(upload.status, StageStatus.FAILED)

    def test_prompt_includes_customer_brief_not_hardcoded_only(self):
        cfg = dict(CFG)
        cfg["brief"] = "Klinik için sakin bir tanıtım"
        cfg["content_type"] = "klinik videosu"
        cfg["voiceover"] = True
        job = create_job_order(cfg, [])
        self.assertIn("Klinik için sakin bir tanıtım", job.generated_prompt)
        self.assertIn("klinik videosu", job.generated_prompt)
        self.assertIn("Türkçe seslendirme", job.generated_prompt)

    def test_sanitize_log_strips_query_and_secrets(self):
        line = sanitize_log_line(
            "src=https://video.google.com/file?token=abc123 cookie=xyz Authorization: Bearer tok"
        )
        self.assertNotIn("abc123", line)
        self.assertNotIn("xyz", line)
        self.assertNotIn("tok", line)

    def test_pipeline_creates_standard_folder_structure(self):
        cfg = dict(CFG)
        lines, log = collect()
        provider = FakeProvider()
        report = run_content_workflow(cfg, [], log, provider=provider)
        base = Path(report.job.output_dir)
        self.assertTrue(base.is_dir())
        self.assertTrue((base / "01_Senaryo" / "tam_senaryo.txt").is_file())
        self.assertTrue((base / "01_Senaryo" / "sahne_plani.json").is_file())
        self.assertTrue((base / "02_Gorseller").is_dir())
        self.assertTrue((base / "03_Videolar").is_dir())

    def test_live_pipeline_executes_stages_sequentially(self):
        cfg = dict(CFG)
        cfg["dry_run"] = False
        cfg["target_folder_id"] = "folder-123"
        lines, log = collect()
        provider = FakeProvider(
            response_text="[SAHNE 1]\nBAŞLIK: Giriş\nGÖRSEL_PROMPT: Product on podium\nVIDEO_PROMPT: Camera zoom in\nSESLENDIRME: Hoşgeldiniz"
        )
        report = run_content_workflow(cfg, ["ref.png"], log, provider=provider)
        self.assertEqual(report.overall_status, "success")
        self.assertTrue(report.delivered)
        # Sahneler ayrıştırılmış olmalı
        self.assertEqual(len(report.job.scenes), 1)
        self.assertEqual(report.job.scenes[0].title, "Giriş")
        # Promptlar sırayla gitmiş olmalı (breakdown, görsel, video)
        self.assertGreaterEqual(len(provider.sent_prompts), 3)

    def test_video_production_attaches_scene_image(self):
        cfg = dict(CFG)
        cfg["dry_run"] = False
        cfg["attach_scene_image_to_video"] = True
        lines, log = collect()
        provider = FakeProvider(
            response_text="[SAHNE 1]\nBAŞLIK: Giriş\nGÖRSEL_PROMPT: Product on podium\nVIDEO_PROMPT: Camera zoom in\nSESLENDIRME: Hoşgeldiniz"
        )
        report = run_content_workflow(cfg, [], log, provider=provider)
        self.assertEqual(report.overall_status, "success")
        # Video üretimi öncesinde sahne görseli attach edilmiş olmalı
        self.assertIn("attach_references", provider.calls)
        # Video promptu sahne görselini animate etmesini istemeli
        video_prompt = provider.sent_prompts[-1]
        self.assertIn("Animate the attached reference image", video_prompt)

    def test_pipeline_merges_videos_and_uploads_all_folders(self):
        cfg = dict(CFG)
        cfg["dry_run"] = False
        cfg["scene_count"] = 2
        lines, log = collect()
        provider = FakeProvider(
            new_videos=["https://example.invalid/v1.mp4", "https://example.invalid/v2.mp4"],
            response_text=(
                "[SAHNE 1]\nBAŞLIK: Giriş\nGÖRSEL_PROMPT: Image 1\nVIDEO_PROMPT: Video 1\nSESLENDIRME: Text 1\n\n"
                "[SAHNE 2]\nBAŞLIK: Kapanış\nGÖRSEL_PROMPT: Image 2\nVIDEO_PROMPT: Video 2\nSESLENDIRME: Text 2"
            ),
        )
        report = run_content_workflow(cfg, [], log, provider=provider)
        self.assertEqual(report.overall_status, "success")
        self.assertTrue(report.delivered)
        self.assertIn("upload_all_outputs_to_drive", provider.calls)
        # Drive upload aşaması klasörleri içermeli
        drive_stage = next(s for s in report.stages if s.name == "drive_upload")
        self.assertEqual(drive_stage.status, StageStatus.SUCCESS)
        self.assertIn("03_Videolar", drive_stage.details.get("folders", []))

    def test_live_workflow_inherits_root_drive_folder_when_target_id_omitted(self):
        cfg = dict(CFG)
        cfg["dry_run"] = False
        cfg["target_folder_id"] = ""
        cfg["root_drive_folder"] = "https://drive.google.com/drive/folders/global-root-folder"
        cfg["brief"] = "Mavi Termos için reklam videosu"
        lines, log = collect()
        provider = FakeProvider(
            new_videos=["https://example.invalid/v1.mp4"],
            response_text=(
                "[SAHNE 1]\nBAŞLIK: Ürün Gösterimi\nGÖRSEL_PROMPT: Blue bottle\nVIDEO_PROMPT: 360 spin\nSESLENDIRME: Mavi Termos"
            ),
        )
        report = run_content_workflow(cfg, [], log, provider=provider)
        self.assertEqual(report.overall_status, "success")
        self.assertTrue(report.delivered)
        self.assertEqual(report.job.target_folder_id, "global-root-folder")
        drive_stage = next(s for s in report.stages if s.name == "drive_upload")
        self.assertEqual(drive_stage.status, StageStatus.SUCCESS)
    def test_sanitize_gem_url_strips_chat_conversation_id(self):
        from providers.gemini import sanitize_gem_url
        raw = "https://gemini.google.com/gem/af11977bbdce/7761df24e0f49722"
        self.assertEqual(sanitize_gem_url(raw), "https://gemini.google.com/gem/af11977bbdce")
        self.assertEqual(sanitize_gem_url("https://gemini.google.com/gem/af11977bbdce"), "https://gemini.google.com/gem/af11977bbdce")

    def test_live_workflow_calls_reset_chat_session(self):
        cfg = dict(CFG)
        cfg["dry_run"] = False
        lines, log = collect()
        provider = FakeProvider(new_videos=["https://example.invalid/v1.mp4"])
        report = run_content_workflow(cfg, [], log, provider=provider)
        self.assertIn("reset_chat_session", provider.calls)

    def test_plain_brief_zero_refs_builds_original_scene_prompts(self):
        from services.prompt_builder import build_script_breakdown_prompt, build_scene_video_prompt
        cfg = dict(CFG)
        cfg["brief"] = "reklam videosu üret"
        cfg["scenario"] = ""
        job = create_job_order(cfg, [])
        breakdown_prompt = build_script_breakdown_prompt(job)
        self.assertIn("sıfırdan tamamen özgün", breakdown_prompt)
        self.assertIn("reklam videosu üret", breakdown_prompt)

        scene = job.scenes[0] if job.scenes else None
        if not scene:
            from models.job import SceneItem
            scene = SceneItem(index=1, title="Sahne 1", duration_seconds=5, video_prompt="Dynamic camera pan")
        vid_p = build_scene_video_prompt(scene, job, has_image=False)
        self.assertNotIn("reference image", vid_p)
        self.assertIn("faithful to the product design", vid_p)

    def test_scan_and_unique(self):
        from services.files import scan_reference_images, unique_existing_images
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "a.png").write_bytes(b"png")
            (folder / "b.jpg").write_bytes(b"jpg")
            (folder / "c.txt").write_text("no")
            found = scan_reference_images(folder, limit=4)
            self.assertEqual(len(found), 2)
            merged = unique_existing_images([*found, found[0]], limit=4)
            self.assertEqual(len(merged), 2)

    def test_reference_images_attached_for_scene_image_generation(self):
        import tempfile
        from services.prompt_builder import build_scene_image_prompt, build_scene_video_prompt

        with tempfile.TemporaryDirectory() as tmp:
            ref_path = Path(tmp) / "ref_avatar.png"
            ref_path.write_bytes(b"dummy_ref")

            cfg = dict(CFG)
            cfg["dry_run"] = False
            cfg["scene_count"] = 2
            lines, log = collect()
            provider = FakeProvider(
                new_videos=["https://example.invalid/v1.mp4", "https://example.invalid/v2.mp4"],
                response_text=(
                    "[SAHNE 1]\nBAŞLIK: Sahne 1\nGÖRSEL_PROMPT: Image 1\nVIDEO_PROMPT: Video 1\nSESLENDIRME: Text 1\n\n"
                    "[SAHNE 2]\nBAŞLIK: Sahne 2\nGÖRSEL_PROMPT: Image 2\nVIDEO_PROMPT: Video 2\nSESLENDIRME: Text 2"
                ),
            )
            report = run_content_workflow(cfg, [str(ref_path)], log, provider=provider)
            self.assertEqual(report.overall_status, "success")

            # attach_references birden çok kez çağrılmalı:
            # 1 kez ilk bağlantı/Stage 1, 2 kez Stage 2 sahne görselleri için, 2 kez Stage 3 videolar için
            attach_count = provider.calls.count("attach_references")
            self.assertGreaterEqual(attach_count, 3)

            # Prompt kontrolleri
            job = report.job
            img_prompt = build_scene_image_prompt(job.scenes[0], job)
            self.assertIn("CRITICAL FIDELITY REQUIREMENT", img_prompt)
            self.assertIn("replicate and maintain the exact appearance", img_prompt)

            vid_prompt = build_scene_video_prompt(job.scenes[1], job, has_image=True)
            self.assertIn("STANDALONE SCENE INSTRUCTION", vid_prompt)
            self.assertIn("Do NOT treat this as a leftover continuation", vid_prompt)

    def test_animation_content_type_enforces_3d_cartoon_pixar_style(self):
        from services.prompt_builder import (
            build_script_breakdown_prompt,
            build_scene_image_prompt,
            build_scene_video_prompt,
            build_production_prompt,
            is_animation_content_type,
        )
        cfg = dict(CFG)
        cfg["content_type"] = "animasyon"
        cfg["brief"] = "okul öncesi eğitici video"  # Brief veya açıklamada pixar/3d yazmıyor
        job = create_job_order(cfg, [])

        self.assertTrue(is_animation_content_type(job))

        # 1. Senaryo planlama promptu 3D Pixar çizgi film stilini zorunlu kılmalı
        breakdown_prompt = build_script_breakdown_prompt(job)
        self.assertIn("3D Cartoon Animation, Pixar Style", breakdown_prompt)
        self.assertIn("Pixar ve Disney 3D animasyon tarzı", breakdown_prompt)

        # 2. Sahne görsel üretim promptu
        from models.job import SceneItem
        scene = SceneItem(
            index=1,
            title="Sahne 1",
            duration_seconds=5,
            image_prompt="Two cute kids playing with wooden toys",
            video_prompt="Camera panning around the kids",
        )
        img_prompt = build_scene_image_prompt(scene, job)
        self.assertIn("3D cartoon animation Pixar style", img_prompt)
        self.assertIn("Pixar style", img_prompt)

        # 3. Sahne video üretim promptu
        vid_prompt = build_scene_video_prompt(scene, job, has_image=True)
        self.assertIn("3D cartoon animation, Pixar style animation", vid_prompt)

        # 4. Geriye dönük uyumluluk promptu
        prod_prompt = build_production_prompt(job)
        self.assertIn("Pixar tarzı 3D", prod_prompt)

    def test_create_job_order_supports_both_projects(self):
        from models.job import PROJECT_DUYGU, PROJECT_IKIZLER

        # Duygu AI Studio
        cfg_duygu = dict(CFG, project_name=PROJECT_DUYGU)
        job_duygu = create_job_order(cfg_duygu, [])
        self.assertEqual(job_duygu.project_name, PROJECT_DUYGU)

        # İkizler Masal Durağı
        cfg_ikizler = dict(CFG, project_name=PROJECT_IKIZLER, gem_url="https://gemini.google.com/gem/8e63671e6329")
        job_ikizler = create_job_order(cfg_ikizler, [])
        self.assertEqual(job_ikizler.project_name, PROJECT_IKIZLER)
        self.assertEqual(job_ikizler.gem_url, "https://gemini.google.com/gem/8e63671e6329")

    def test_ikizler_masal_duragi_dry_run_workflow(self):
        from models.job import PROJECT_IKIZLER
        cfg_ikizler = dict(
            CFG,
            project_name=PROJECT_IKIZLER,
            gem_url="https://gemini.google.com/gem/8e63671e6329",
            masal_topic="Ormanın Küçük Kahramanı",
            content_type="çocuk masalı",
            scene_count=2,
            seconds_per_scene=5,
            dry_run=True,
        )
        lines, log = collect()
        provider = FakeProvider()
        report = run_content_workflow(cfg_ikizler, [], log, provider=provider)
        self.assertEqual(report.overall_status, "dry_run")
        self.assertEqual(report.job.project_name, PROJECT_IKIZLER)
        self.assertIn("Masal_Ormanın_Küçük_Kahramanı", report.job.brand_name)
        self.assertTrue(report.job.output_dir)
        self.assertTrue(Path(report.job.output_dir).exists())

    def test_video_policy_refusal_detection(self):
        from providers.base import VideoPolicyRefusalError
        from providers.gemini import GeminiWebProvider

        dummy_cfg = {"gem_url": "https://gemini.google.com/gem/test"}
        prov = GeminiWebProvider(dummy_cfg, lambda m: None)

        exact_msg = "I can't generate that video. Try describing another idea. You can also get tips for how to write prompts and review our video policy guidelines"
        with self.assertRaises(VideoPolicyRefusalError):
            prov.check_text_safety_or_limits(exact_msg)

        tr_msg = "Bu videoyu oluşturamıyorum. Başka bir fikir açıklamayı deneyin. Video politikası kurallarımızı inceleyin."
        with self.assertRaises(VideoPolicyRefusalError):
            prov.check_text_safety_or_limits(tr_msg)

    def test_revise_scene_video_prompt_preserves_narrative_and_sanitizes(self):
        from models.job import SceneItem
        from services.prompt_builder import revise_scene_video_prompt_for_safety
        from services.workflow import create_job_order

        job = create_job_order(
            dict(
                CFG,
                content_type="klinik videosu",
                video_format="9:16",
                scene_count=1,
                seconds_per_scene=5,
                brief="Estetik klinik tanıtımı",
            ),
            [],
        )
        scene = SceneItem(
            index=1,
            title="Dudak Dolgusu Uygulaması",
            description="Doktor hastaya enjektör ile dudak dolgusu uygular.",
            video_prompt="Close up of doctor injecting needle with lip filler on patient face",
        )


        revised_prompt, revised_desc = revise_scene_video_prompt_for_safety(scene, job, attempt=1)

        # Bütünlük kontrolü: Sahne indeksi ve stili korunmalı
        self.assertIn("Scene 1 of 1", revised_prompt)
        self.assertIn("9:16 vertical video", revised_prompt)
        # Güvenlik kontrolü: Hassas kelimeler (needle, injecting, lip filler) temizlenmeli
        self.assertNotIn("needle", revised_prompt.lower())
        self.assertNotIn("injecting", revised_prompt.lower())
        self.assertNotIn("lip filler", revised_prompt.lower())
        # Güvenli sinematik alternatif eklenmeli
        self.assertIn("aesthetic clinic", revised_prompt.lower())
        self.assertIn("video policy guidelines", revised_prompt.lower())
        self.assertIn("Politika Uyarlaması 1", revised_desc)

    def test_workflow_retries_and_succeeds_on_video_policy_refusal(self):
        import json
        from providers.base import VideoPolicyRefusalError

        cfg = dict(CFG)
        cfg["dry_run"] = False
        cfg["scene_count"] = 1
        cfg["brief"] = "Klinik için sakin tanıtım"
        cfg["content_type"] = "klinik videosu"

        refusal_msg = "I can't generate that video. Try describing another idea. You can also get tips for how to write prompts and review our video policy guidelines"
        provider = FakeProvider(
            video_errors=[VideoPolicyRefusalError(refusal_msg)],
            new_videos=["https://example.invalid/successful_safe_video.mp4"],
            response_text=(
                "[SAHNE 1]\nBAŞLIK: Giriş\n"
                "GÖRSEL_PROMPT: Modern clinic lounge\n"
                "VIDEO_PROMPT: Close up of doctor injecting dermal filler on patient face\n"
                "SESLENDIRME: Hoş geldiniz."
            ),
        )

        lines, log = collect()
        report = run_content_workflow(cfg, [], log, provider=provider)

        # Süreç başarıyla sonuçlanmalı
        self.assertEqual(report.overall_status, "success")
        self.assertTrue(report.delivered)

        # Loglarda uyarının ve revizyonun yansıdığı doğrulanmalı
        log_text = "\n".join(lines)
        self.assertIn("Gemini video politika uyarısı tespit edildi", log_text)
        self.assertIn("Senaryo bütünlüğü korunarak prompt Gemini kurallarına uygun şekilde revize ediliyor", log_text)
        self.assertIn("Düzenlenen prompt Gemini'ye tekrar gönderiliyor", log_text)

        # Gemini'ye en az 2 prompt gönderilmiş olmalı (senaryo planı + 1. video promptu reddedildi + 2. revize promptu kabul edildi)
        self.assertTrue(len(provider.sent_prompts) >= 3)
        # En son gönderilen video promptu güvenli olmalı
        last_video_prompt = provider.sent_prompts[-1]
        self.assertNotIn("needle", last_video_prompt.lower())
        self.assertNotIn("injecting", last_video_prompt.lower())
        self.assertIn("video policy guidelines", last_video_prompt.lower())

        # Sahne planı dosyasının da güncellendiği teyit edilmeli
        scenarios_dir = Path(report.job.output_dir) / "01_Senaryo"
        plan_json = json.loads((scenarios_dir / "sahne_plani.json").read_text(encoding="utf-8"))
        self.assertEqual(len(plan_json), 1)
        self.assertIn("Politika Uyarlaması", plan_json[0]["description"])

    def test_resume_reuses_existing_scene_plan_without_manual_scenario(self):
        """Kullanıcı hazır senaryo kutusunu boş bırakıp brief ile başlattığında ve süreç kesildiğinde,
        tekrar başlatıldığında mevcut sahne_plani.json korunmalı ve Gemini'ye yeni senaryo promptu gönderilmemeli."""
        import json
        from pathlib import Path
        from services.workflow import run_content_workflow, setup_output_directories, create_job_order
        from models.job import SceneItem, PROJECT_IKIZLER

        cfg = dict(
            CFG,
            project_name=PROJECT_IKIZLER,
            brief="Önceden oluşturulan masal konusu",
            scenario="",  # Kullanıcı elle senaryo girmedi
            content_type="çocuk masalı",
            scene_count=2,
            seconds_per_scene=5,
            dry_run=True,
        )
        job = create_job_order(cfg, [])
        dirs = setup_output_directories(job)
        # Önceden üretilmiş bir senaryo planı olduğunu simüle et
        existing_scenes = [
            SceneItem(index=1, title="Mevcut Sahne 1", description="Açıklama 1", duration_seconds=5, image_prompt="Img 1", video_prompt="Vid 1").to_dict(),
            SceneItem(index=2, title="Mevcut Sahne 2", description="Açıklama 2", duration_seconds=5, image_prompt="Img 2", video_prompt="Vid 2").to_dict(),
        ]
        (dirs["scenarios"] / "sahne_plani.json").write_text(json.dumps(existing_scenes), encoding="utf-8")
        (dirs["scenarios"] / "tam_senaryo.txt").write_text("Mevcut senaryo metni", encoding="utf-8")

        lines, log = collect()
        report = run_content_workflow(cfg, [], log, provider=FakeProvider())
        self.assertEqual(len(report.job.scenes), 2)
        self.assertEqual(report.job.scenes[0].title, "Mevcut Sahne 1")
        self.assertEqual(report.job.scene_plan_source, "reused_existing")

    def test_detect_video_policy_refusal_ignores_unchanged_baseline(self):
        """detect_video_policy_refusal, prompt öncesinde zaten ekranda olan eski yanıt metnini yeni ret saymamalıdır."""
        from providers.gemini import GeminiWebProvider

        prov = GeminiWebProvider({"gem_url": "https://gemini.google.com/gem/test"}, lambda m: None)
        old_refusal = "Bu videoyu oluşturamıyorum. Başka bir fikir açıklamayı deneyin."
        prov._last_prompt_response = old_refusal
        prov.read_response_text = lambda latest_only=True: old_refusal

        # Mock _require_page to avoid playwright requirement
        class DummyPage:
            def locator(self, *args, **kwargs):
                class DummyLoc:
                    def count(self):
                        return 0
                return DummyLoc()

        prov._page = DummyPage()
        # Aynı yanıt kaldığı sürece yeni ret tespit edilmemeli
        self.assertIsNone(prov.detect_video_policy_refusal())

    def test_detect_high_traffic_capacity_trigger(self):
        """Kullanıcının karşılaştığı sunucu yoğunluğu ve tam kapasite hatası HighTrafficCapacityError olarak algılanmalıdır."""
        from providers.base import HighTrafficCapacityError
        from providers.gemini import GeminiWebProvider

        prov = GeminiWebProvider({"gem_url": "https://gemini.google.com/gem/test"}, lambda m: None)
        exact_traffic_err = (
            "I'm sorry, I'm experiencing unusually high traffic right now generating videos and am at full capacity. "
            "Please try again later."
        )
        with self.assertRaises(HighTrafficCapacityError):
            prov.check_text_safety_or_limits(exact_traffic_err)

    def test_video_production_retries_on_high_traffic_capacity(self):
        """Sunucu yoğunluğu uyarısı alındığında politika revizyonu harcanmadan tekrar denenmeli ve video üretildiğinde başarıyla tamamlanmalıdır."""
        from unittest.mock import patch
        from providers.base import HighTrafficCapacityError

        traffic_err = HighTrafficCapacityError("unusually high traffic right now generating videos and am at full capacity")
        provider = FakeProvider(video_errors=[traffic_err, None])
        lines, log = collect()

        with patch("time.sleep", return_value=None):
            report = run_content_workflow(
                dict(CFG, scene_count=1, seconds_per_scene=5, dry_run=False),
                [],
                log,
                provider=provider,
            )

        log_text = "\n".join(lines)
        self.assertIn("Gemini sunucu yoğunluğu tespit edildi", log_text)
        self.assertIn("video üretilene kadar devam edilecek", log_text)
        # 1. denemede yoğunluk hatası alıp 2. denemede videoyu başarıyla kaydetti
        self.assertEqual(report.job.scenes[0].status, "video_done")
        self.assertTrue(report.delivered)

    def test_workflow_selective_regeneration_reuses_existing_assets(self):
        """Kullanıcı belirli bir sahneyi tekrar üretmek istediğinde mevcut diğer görseller ve videolar korunmalı, yalnızca seçilen eksik varlık üretilmelidir."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_base = Path(tmp_dir) / "TestProject"
            scenarios_dir = out_base / "01_Senaryo"
            images_dir = out_base / "02_Gorseller"
            videos_dir = out_base / "03_Videolar"
            scenarios_dir.mkdir(parents=True)
            images_dir.mkdir(parents=True)
            videos_dir.mkdir(parents=True)

            # 2 sahnelik plan
            plan_data = [
                {
                    "index": 1,
                    "title": "Sahne 1",
                    "duration_seconds": 5,
                    "voiceover_text": "Ses 1",
                    "image_prompt": "Görsel 1",
                    "video_prompt": "Video 1",
                    "image_path": str(images_dir / "sahne_1.png"),
                    "video_path": str(videos_dir / "sahne_1.mp4"),
                    "status": "video_done",
                },
                {
                    "index": 2,
                    "title": "Sahne 2",
                    "duration_seconds": 5,
                    "voiceover_text": "Ses 2",
                    "image_prompt": "Görsel 2 Düzenlenmiş",
                    "video_prompt": "Video 2 Düzenlenmiş",
                    "image_path": str(images_dir / "sahne_2.png"),
                    "video_path": "",
                    "status": "pending",
                },
            ]
            (scenarios_dir / "sahne_plani.json").write_text(json.dumps(plan_data), encoding="utf-8")
            (scenarios_dir / "tam_senaryo.txt").write_text("Test senaryo", encoding="utf-8")

            # Sahne 1: Görsel ve Video mevcut
            (images_dir / "sahne_1.png").write_bytes(b"A" * 30000)
            (videos_dir / "sahne_1.mp4").write_bytes(b"B" * 200000)

            # Sahne 2: Görsel mevcut, video tekrar üretilmek üzere kullanıcı tarafından seçilmiş (silinmiş/yedeklenmiş)
            (images_dir / "sahne_2.png").write_bytes(b"C" * 30000)

            provider = FakeProvider()
            lines, log = collect()

            report = run_content_workflow(
                dict(
                    CFG,
                    output_dir=str(out_base),
                    scene_plan_source="reused_existing",
                    scene_count=2,
                    dry_run=False,
                ),
                [],
                log,
                provider=provider,
            )

            log_text = "\n".join(lines)
            # Mevcut plan korundu
            self.assertIn("Mevcut senaryo planı korundu (2 sahne)", log_text)
            # Mevcut görseller korundu
            self.assertIn("Mevcut Gemini üretimi görsel korundu: sahne_1.png", log_text)
            self.assertIn("Mevcut Gemini üretimi görsel korundu: sahne_2.png", log_text)
            # Sahne 1 videosu korundu
            self.assertIn("Mevcut video korundu: sahne_1.mp4", log_text)
            # Sadece Sahne 2 videosu üretildi
            self.assertIn("[Sahne 2/2] Video üretimi başlatılıyor", log_text)
            self.assertIn("sahne_2.mp4", log_text)
            # Provider prompt sayısı: Sadece sahne 2 videosu için prompt atıldı
            self.assertEqual(len(provider.sent_prompts), 1)
            self.assertIn("Video 2 Düzenlenmiş", provider.sent_prompts[0])
            self.assertTrue(report.delivered)

    def test_workflow_selective_target_scenes_ignores_unselected_scenes(self):
        """target_video_scenes belirtildiğinde, listede olmayan sahneler eksik olsa bile o sahneler için prompt gönderilmemelidir."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_base = Path(tmp_dir) / "TestProject2"
            scenarios_dir = out_base / "01_Senaryo"
            images_dir = out_base / "02_Gorseller"
            videos_dir = out_base / "03_Videolar"
            scenarios_dir.mkdir(parents=True)
            images_dir.mkdir(parents=True)
            videos_dir.mkdir(parents=True)

            plan_data = [
                {
                    "index": 1,
                    "title": "Sahne 1",
                    "duration_seconds": 5,
                    "voiceover_text": "Ses 1",
                    "image_prompt": "Görsel 1",
                    "video_prompt": "Video 1",
                    "image_path": str(images_dir / "sahne_1.png"),
                    "video_path": str(videos_dir / "sahne_1.mp4"),
                    "status": "video_done",
                },
                {
                    "index": 2,
                    "title": "Sahne 2",
                    "duration_seconds": 5,
                    "voiceover_text": "Ses 2",
                    "image_prompt": "Görsel 2",
                    "video_prompt": "Video 2",
                    "image_path": str(images_dir / "sahne_2.png"),
                    "video_path": "",
                    "status": "pending",
                },
                {
                    "index": 3,
                    "title": "Sahne 3",
                    "duration_seconds": 5,
                    "voiceover_text": "Ses 3",
                    "image_prompt": "Görsel 3",
                    "video_prompt": "Video 3",
                    "image_path": str(images_dir / "sahne_3.png"),
                    "video_path": "",
                    "status": "pending",
                },
            ]
            (scenarios_dir / "sahne_plani.json").write_text(json.dumps(plan_data), encoding="utf-8")
            (images_dir / "sahne_1.png").write_bytes(b"A" * 30000)
            (images_dir / "sahne_2.png").write_bytes(b"B" * 30000)
            (images_dir / "sahne_3.png").write_bytes(b"C" * 30000)
            (videos_dir / "sahne_1.mp4").write_bytes(b"V" * 200000)

            provider = FakeProvider()
            lines, log = collect()

            # Kullanıcı yalnızca Sahne 2 videosunu üretmek istedi (Sahne 3 seçilmedi)
            report = run_content_workflow(
                dict(
                    CFG,
                    output_dir=str(out_base),
                    scene_plan_source="reused_existing",
                    scene_count=3,
                    target_video_scenes=[2],
                    target_image_scenes=[],
                    dry_run=False,
                ),
                [],
                log,
                provider=provider,
            )

            # Sadece Sahne 2 videosu üretilmeli, Sahne 3 videosu istenmediği için üretilmemeli
            self.assertEqual(len(provider.sent_prompts), 1)
            self.assertIn("Video 2", provider.sent_prompts[0])


    def test_gemini_text_model_and_programming_refusals_detected_as_safety_refusal(self):
        """Gemini'nin 'Ben yalnızca metin oluşturuyorum' ve 'yalnızca bir dil modeliyim' yanıtları SafetyRefusalError olarak yakalanmalıdır."""
        from providers.base import SafetyRefusalError
        from providers.gemini import GeminiWebProvider

        prov = GeminiWebProvider({"gem_url": "https://gemini.google.com/gem/test"}, lambda m: None)
        err1 = "İsteğinizi gerçekleştirebilecek şekilde programlanmadım. Ben yalnızca metin oluşturuyorum."
        err2 = "Ben yalnızca bir dil modeli olduğum ve gerekli bilgiye ya da beceriye sahip olmadığım için bu isteğinize yardımcı olamıyorum."

        with self.assertRaises(SafetyRefusalError):
            prov.check_text_safety_or_limits(err1)

        with self.assertRaises(SafetyRefusalError):
            prov.check_text_safety_or_limits(err2)

    def test_sanitize_topic_for_prompts_strips_meta_commands(self):
        """Brief'teki 'ugc içeriği üret', 'avatar karakter' gibi meta ifadeler temizlenmelidir."""
        from services.prompt_builder import sanitize_topic_for_prompts

        raw = "ugc içeriği üret çocuk giyim üzerine avatar karakter 4-5 yaşlarında küçük bir kız çocuğu renkli çorapları tanıtabilir"
        cleaned = sanitize_topic_for_prompts(raw)
        self.assertNotIn("ugc içeriği", cleaned.lower())
        self.assertNotIn("avatar karakter", cleaned.lower())
        self.assertIn("çocuk giyim", cleaned)
        self.assertIn("renkli çorapları", cleaned)

    def test_parse_scene_breakdown_refusal_text_uses_fallback_scenes(self):
        """Gemini metin modeli uyarısı verdiğinde ham brief görsel promptu yapılmamalı, temiz fallback sahneleri üretilmelidir."""
        from services.prompt_builder import parse_scene_breakdown

        refusal = "Ben yalnızca bir dil modeli olduğum için bu isteğinize yardımcı olamıyorum."
        brief = "ugc içeriği üret çocuk giyim üzerine avatar karakter 4-5 yaşlarında küçük bir kız çocuğu renkli çorapları tanıtabilir"

        scenes = parse_scene_breakdown(refusal, default_count=1, default_seconds=10, fallback_prompt=brief, is_animation=False)
        self.assertEqual(len(scenes), 1)
        # Ham brief görsel promptu olmamalı
        self.assertNotEqual(scenes[0].image_prompt, brief)
        self.assertIn("Commercial product photography", scenes[0].image_prompt)
        self.assertIn("9:16 vertical orientation", scenes[0].image_prompt)

    def test_video_production_attaches_generated_image(self):
        """Üretilen sahne görseli video üretiminde eke konularak 'Animate the attached reference image' ile gönderilmelidir."""
        cfg = dict(CFG)
        cfg["dry_run"] = False
        lines, log = collect()
        provider = FakeProvider(
            response_text="[SAHNE 1]\nBAŞLIK: Giriş\nGÖRSEL_PROMPT: Product on podium\nVIDEO_PROMPT: Camera zoom in\nSESLENDIRME: Hoşgeldiniz"
        )
        report = run_content_workflow(cfg, [], log, provider=provider)
        self.assertEqual(report.overall_status, "success")
        video_prompt = provider.sent_prompts[-1]
        self.assertTrue(video_prompt.startswith("Animate the attached reference image"))
        self.assertIn("attach_references", provider.calls)


if __name__ == "__main__":
    unittest.main()
