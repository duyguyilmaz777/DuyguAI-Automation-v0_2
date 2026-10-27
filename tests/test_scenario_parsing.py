"""Gemini senaryo yanıtının ayrıştırılması için regresyon testleri.

Gemini arayüzünden okunan metinde alan etiketleri bazen birbirine yapışık gelir
(örn. "Başında!AÇIKLAMA:") ve başta Gem adı / "şunu dedi:" satırları bulunur.
"""
import unittest

from services.prompt_builder import (
    clean_model_response_text,
    has_scene_markers,
    parse_scene_breakdown,
)

GEMINI_RAW = (
    "İkizler Masal Durağı — Animasyon ve Masal Yönetmeni\n"
    "Özel Gem\n"
    "İkizler Masal Durağı — Animasyon ve Masal Yönetmeni şunu dedi:\n\n"
    "[SAHNE 1]\n"
    "BAŞLIK: Meraklı İkizler İş Başında!AÇIKLAMA: Atakan ve Kumsal süte bakarlar."
    "GÖRSEL_PROMPT: 3D cartoon animation, Pixar style. Twins at a table. 9:16 vertical video."
    "VIDEO_PROMPT: 3D cartoon animation, Pixar style motion. Camera moves closer. 9:16 vertical video."
    'SESLENDIRME: (Dış Ses): "Acaba şimdi ne olacak?"\n\n'
    "[SAHNE 2]\n"
    "BAŞLIK: Renklerin Dansı!AÇIKLAMA: Renkler dans eder."
    "GÖRSEL_PROMPT: 3D cartoon animation, Pixar style. Swirling colors in milk."
    "VIDEO_PROMPT: 3D cartoon animation, Pixar style motion. Colors spread.SESLENDIRME:\n"
    'Atakan: "Oooo! Kumsal bak, kaçıyorlar!"\n'
    'Kumsal: "Çok güzel! Renkler dans ediyor Atakan!"\n'
)


class GeminiScenarioParsingTests(unittest.TestCase):
    def test_glued_fields_are_parsed_exactly(self):
        scenes = parse_scene_breakdown(GEMINI_RAW, default_count=2, default_seconds=10, is_animation=True)
        self.assertEqual(len(scenes), 2)
        self.assertEqual(scenes[0].title, "Meraklı İkizler İş Başında!")
        self.assertEqual(scenes[0].description, "Atakan ve Kumsal süte bakarlar.")
        self.assertTrue(scenes[0].image_prompt.startswith("3D cartoon animation, Pixar style. Twins"))
        self.assertTrue(scenes[0].video_prompt.endswith("9:16 vertical video."))
        self.assertEqual(scenes[0].voiceover_text, '(Dış Ses): "Acaba şimdi ne olacak?"')
        self.assertEqual(scenes[1].title, "Renklerin Dansı!")
        self.assertEqual(scenes[1].video_prompt, "3D cartoon animation, Pixar style motion. Colors spread.")
        self.assertIn("Atakan:", scenes[1].voiceover_text)
        self.assertIn("Kumsal:", scenes[1].voiceover_text)

    def test_no_fallback_template_used(self):
        scenes = parse_scene_breakdown(GEMINI_RAW, default_count=2, is_animation=True)
        self.assertFalse(any("Meraklı Karşılaşma" in s.title for s in scenes))

    def test_cleaning_strips_gem_header_and_splits_fields(self):
        cleaned = clean_model_response_text(GEMINI_RAW)
        self.assertTrue(cleaned.startswith("[SAHNE 1]"))
        self.assertIn("İş Başında!\nAÇIKLAMA:", cleaned)
        self.assertNotIn("şunu dedi", cleaned)
        self.assertTrue(has_scene_markers(cleaned))

    def test_cleaning_strips_introductory_chat_before_scene_one(self):
        noisy_gemini = (
            "Ben metin tabanlı bir yapay zeka asistanıyım. Harika bir deney konusu! İşte senaryonuz:\n\n"
            "[SAHNE 1]\n"
            "BAŞLIK: Süt Deneyi\n"
            "AÇIKLAMA: Deney başlar.\n"
            "GÖRSEL_PROMPT: 3D cartoon animation, Pixar style.\n"
            "VIDEO_PROMPT: 3D cartoon animation.\n"
            "SESLENDIRME: Bakalım ne olacak?\n"
        )
        cleaned = clean_model_response_text(noisy_gemini)
        self.assertTrue(cleaned.startswith("[SAHNE 1]"), f"Temizlenen metin [SAHNE 1] ile başlamalı: {cleaned}")
        self.assertNotIn("Ben metin tabanlı", cleaned)

    def test_sanitize_script_task_input_removes_video_generation_phrases(self):
        from services.prompt_builder import sanitize_script_task_input

        raw_brief = "ikizler ile deneyler serisi yapalım örnek bir deney kurgula ve videosunu üret"
        sanitized = sanitize_script_task_input(raw_brief)
        self.assertNotIn("videosunu üret", sanitized)
        self.assertIn("ikizler ile deneyler serisi", sanitized)

    def test_build_script_breakdown_prompt_retry_mode(self):
        from services.prompt_builder import build_script_breakdown_prompt
        from services.workflow import create_job_order

        job = create_job_order(
            {
                "brief": "ikizler ile renk deneyi",
                "scene_count": "2",
                "seconds_per_scene": "5",
                "content_type": "animasyon masal",
                "project_name": "İkizler Masal Durağı",
            },
            [],
        )
        retry_prompt = build_script_breakdown_prompt(job, retry_attempt=2)
        self.assertIn("DOĞRUDAN aşağıdaki [SAHNE 1] formatında", retry_prompt)
        self.assertIn("İSTENMEMEKTEDİR", retry_prompt)

    def test_looks_like_scene_plan_in_gemini_provider(self):
        from providers.gemini import GeminiWebProvider

        valid_plan = (
            "Ben metin tabanlı bir yapay zekayım ama işte sahne planı:\n"
            "[SAHNE 1]\n"
            "BAŞLIK: Deney Başlıyor\n"
            "AÇIKLAMA: Renkler süte damlatılır.\n"
            "GÖRSEL_PROMPT: 3D cartoon animation Pixar style.\n"
            "VIDEO_PROMPT: Smooth camera motion.\n"
        )
        self.assertTrue(GeminiWebProvider._looks_like_scene_plan(valid_plan))

    def test_workflow_integrates_dialogue_into_video_prompt(self):
        """Senaryodaki seslendirme/diyalog metninin sahne video promptuna eklendiğini doğrula."""
        from services.prompt_builder import clean_voiceover_for_prompt, build_scene_video_prompt
        from models.job import SceneItem, JobOrder

        vo_raw = 'Atakan: "Kumsal bak! Şimdi sarı ve mavi suyu yavaşça kâseye dökelim." \nAtakanKumsalAvatar.PNG'
        cleaned = clean_voiceover_for_prompt(vo_raw)
        self.assertEqual(cleaned, 'Atakan: "Kumsal bak! Şimdi sarı ve mavi suyu yavaşça kâseye dökelim."')
        self.assertNotIn("AtakanKumsalAvatar.PNG", cleaned)

        sc = SceneItem(
            index=1,
            title="Giriş",
            description="Deney",
            duration_seconds=10,
            voiceover_text=cleaned,
            image_prompt="3D Pixar twins",
            video_prompt="Camera moves closer.",
            status="pending",
        )
        job = JobOrder(
            job_id="test_dlg",
            brand_name="Test",
            brief="Brief",
            scenario="Scenario",
            content_type="animasyon",
            video_format="9:16",
            scene_count=1,
            seconds_per_scene=10,
            voiceover=True,
            music=False,
            editing_notes="",
            extra_notes="",
            target_folder_id="drive_id",
            gem_url="gem_url",
            reference_images=[],
        )
        vid_p = build_scene_video_prompt(sc, job)
        self.assertIn("Camera moves closer.", vid_p)
        self.assertIn("3D cartoon animation", vid_p)
        self.assertIn("9:16 vertical video", vid_p)


if __name__ == "__main__":
    unittest.main()
