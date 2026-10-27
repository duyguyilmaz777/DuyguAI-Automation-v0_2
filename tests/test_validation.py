import unittest

from services.validation import validate_job_inputs


BASE = dict(
    gem_url="https://gemini.google.com/gem/test",
    target_folder_id="folder-id",
    content_type="ürün reklamı",
    video_format="9:16",
    scene_count=2,
    seconds_per_scene=5,
    dry_run=True,
)


class ValidationTests(unittest.TestCase):
    def test_brief_and_refs_ok(self):
        result = validate_job_inputs(brief="Kırmızı shaker tanıt", reference_images=["a.png"], **BASE)
        self.assertTrue(result.ok)

    def test_scenario_only_without_brief_ok_with_warning(self):
        result = validate_job_inputs(scenario="Sahne 1: ürün masada", **BASE)
        self.assertTrue(result.ok)
        self.assertTrue(any("brief" in w.lower() or "açıklama" in w.lower() for w in result.warnings))

    def test_no_refs_allowed_when_brief_present(self):
        result = validate_job_inputs(brief="Sadece metinle üret", reference_images=[], **BASE)
        self.assertTrue(result.ok)
        self.assertTrue(any("Referans" in w for w in result.warnings))

    def test_missing_brief_and_scenario_rejected(self):
        result = validate_job_inputs(brief="", scenario="", extra_notes="", **BASE)
        self.assertFalse(result.ok)
        self.assertTrue(any("en az biri" in e for e in result.errors))

    def test_invalid_scene_count(self):
        result = validate_job_inputs(brief="x", scene_count=0, **{k: v for k, v in BASE.items() if k != "scene_count"})
        self.assertFalse(result.ok)

    def test_live_run_requires_drive_folder(self):
        args = dict(BASE)
        args["dry_run"] = False
        args["target_folder_id"] = ""
        result = validate_job_inputs(brief="x", **args)
        self.assertFalse(result.ok)
        self.assertTrue(any("Ana Google Drive Klasörü" in e for e in result.errors))

    def test_live_run_succeeds_with_root_drive_folder_when_target_id_is_empty(self):
        args = dict(BASE)
        args["dry_run"] = False
        args["target_folder_id"] = ""
        args["root_drive_folder"] = "root-folder-123"
        result = validate_job_inputs(brief="x", **args)
        self.assertTrue(result.ok)

    def test_live_run_succeeds_with_drive_url_in_root_folder(self):
        args = dict(BASE)
        args["dry_run"] = False
        args["target_folder_id"] = ""
        args["root_drive_folder"] = "https://drive.google.com/drive/u/0/folders/1hFZbJ4BiN0QhEMFhOAIS1gEhNAlJZqwy?usp=sharing"
        result = validate_job_inputs(brief="x", **args)
        self.assertTrue(result.ok)

    def test_extract_drive_folder_id_helper(self):
        from services.files import extract_drive_folder_id

        self.assertEqual(
            extract_drive_folder_id("https://drive.google.com/drive/folders/1hFZbJ4BiN0QhEMFhOAIS1gEhNAlJZqwy"),
            "1hFZbJ4BiN0QhEMFhOAIS1gEhNAlJZqwy",
        )
        self.assertEqual(
            extract_drive_folder_id("https://drive.google.com/drive/u/0/folders/1hFZbJ4BiN0QhEMFhOAIS1gEhNAlJZqwy?usp=sharing"),
            "1hFZbJ4BiN0QhEMFhOAIS1gEhNAlJZqwy",
        )
        self.assertEqual(
            extract_drive_folder_id("https://drive.google.com/open?id=1hFZbJ4BiN0QhEMFhOAIS1gEhNAlJZqwy"),
            "1hFZbJ4BiN0QhEMFhOAIS1gEhNAlJZqwy",
        )
        self.assertEqual(
            extract_drive_folder_id("  1hFZbJ4BiN0QhEMFhOAIS1gEhNAlJZqwy  "),
            "1hFZbJ4BiN0QhEMFhOAIS1gEhNAlJZqwy",
        )
        self.assertEqual(extract_drive_folder_id(""), "")

    def test_duygu_ai_enforces_commercial_categories(self):
        from models.job import PROJECT_DUYGU, CONTENT_TYPES

        for cat in CONTENT_TYPES:
            args = dict(BASE, content_type=cat, project_name=PROJECT_DUYGU)
            res = validate_job_inputs(brief="Geçerli brief", **args)
            self.assertTrue(res.ok, f"Kategori geçerli olmalı: {cat}")

        # Geçersiz kategori Duygu AI için hata vermeli
        invalid_args = dict(BASE, content_type="masal", project_name=PROJECT_DUYGU)
        res_invalid = validate_job_inputs(brief="Geçerli brief", **invalid_args)
        self.assertFalse(res_invalid.ok)
        self.assertTrue(any("Geçersiz içerik türü" in e for e in res_invalid.errors))

    def test_ikizler_masal_duragi_does_not_force_commercial_categories(self):
        from models.job import PROJECT_IKIZLER

        args = dict(BASE, content_type="çocuk masalı", project_name=PROJECT_IKIZLER)
        res = validate_job_inputs(brief="Tavşan ve kaplumbağa masalı", **args)
        self.assertTrue(res.ok)
        self.assertFalse(any("Geçersiz içerik türü" in e for e in res.errors))

    def test_ikizler_masal_duragi_accepts_reference_images(self):
        from models.job import PROJECT_IKIZLER
        from services.workflow import create_job_order
        from services.prompt_builder import build_script_breakdown_prompt, build_scene_image_prompt

        args = dict(BASE, content_type="çocuk masalı", project_name=PROJECT_IKIZLER)
        res = validate_job_inputs(brief="Minik Tavşan Pamuk'un Maceraları", reference_images=["/tmp/pamuk.png"], **args)
        self.assertTrue(res.ok)
        self.assertEqual(len(res.errors), 0)

        cfg = dict(
            brief="Minik Tavşan Pamuk'un Maceraları",
            content_type="çocuk masalı",
            project_name=PROJECT_IKIZLER,
            scene_count=2,
            seconds_per_scene=5,
            video_format="9:16",
            gem_url="https://gemini.google.com/gem/8e63671e6329",
            dry_run=True,
        )
        job = create_job_order(cfg, ["/tmp/pamuk.png"])
        self.assertEqual(job.reference_images, ["/tmp/pamuk.png"])
        self.assertEqual(job.project_name, PROJECT_IKIZLER)

        # Check script breakdown prompt adapts to masal/character reference
        breakdown_prompt = build_script_breakdown_prompt(job)
        self.assertIn("Müşteri karakter/masal referans görsel(ler)i yükledi", breakdown_prompt)
        self.assertIn("3D Pixar", breakdown_prompt)

        # Check scene image prompt adapts to masal/character reference
        from models.job import SceneItem
        sc = SceneItem(index=1, title="Sahne 1", image_prompt="Pamuk çimlerde koşuyor")
        img_prompt = build_scene_image_prompt(sc, job)
        self.assertIn("CRITICAL FIDELITY & STYLE REQUIREMENT", img_prompt)
        self.assertIn("Pixar", img_prompt)


if __name__ == "__main__":
    unittest.main()

