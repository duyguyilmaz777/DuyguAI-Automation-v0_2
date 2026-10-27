import shutil
import tempfile
from pathlib import Path

from models.job import PROJECT_DUYGU, PROJECT_IKIZLER, SceneItem
from services.social_media import (
    clean_short_summary,
    extract_or_create_cover_image,
    generate_instagram_post,
    prepare_instagram_package,
    strip_emojis_and_icons,
)
from services.workflow import create_job_order


def test_strip_emojis_and_icons():
    raw = "🔥 Harika Ürün! ✨ Çok kullanışlı 👍🚀 #trend"
    cleaned = strip_emojis_and_icons(raw)
    assert "🔥" not in cleaned
    assert "✨" not in cleaned
    assert "👍" not in cleaned
    assert "🚀" not in cleaned
    assert "Harika Ürün! Çok kullanışlı #trend" in cleaned


def test_clean_short_summary():
    text = "Bu video yapay zeka ile üretilmiştir. Merhaba arkadaşlar! Ürün çok kalitelidir. Ergonomik tasarımı ile dikkat çeker. Uzun lafın kısası harika."
    summary = clean_short_summary(text, max_sentences=2)
    assert "yapay zeka" not in summary.lower()
    assert "merhaba arkadaşlar" not in summary.lower()
    sentences = [s for s in summary.split(".") if s.strip()]
    assert len(sentences) <= 2


def test_ikizler_instagram_post_contains_mandatory_hashtags_and_no_emojis():
    cfg = {
        "project_name": PROJECT_IKIZLER,
        "brand_name": "İkizler Masal Durağı",
        "brief": "Masal Konusu: Ormanda Paylaşmanın Gücü\nEğitsel Hedef: Arkadaşlarla paylaşma bilinci",
        "content_type": "Masal / Animasyon",
        "scene_count": 2,
        "seconds_per_scene": 5,
        "video_format": "9:16",
        "gem_url": "https://gemini.google.com/gem/test",
    }
    job = create_job_order(cfg, [])
    job.scenes = [
        SceneItem(index=1, title="Ormana Giriş", description="Sevimli tavşan ve sincap karşılaşır"),
        SceneItem(index=2, title="Paylaşım Anı", description="Elmalar dostça paylaşılır"),
    ]
    post = generate_instagram_post(job, job.scenes)

    assert len(post["hashtags"]) == 5
    assert "#okulöncesieğitim" in post["hashtags"]
    assert "#egiticiiçerik" in post["hashtags"]

    # Emojiler bulunmamalı
    assert strip_emojis_and_icons(post["title"]) == post["title"]
    assert strip_emojis_and_icons(post["description"]) == post["description"]
    for tag in post["hashtags"]:
        assert strip_emojis_and_icons(tag) == tag
        assert tag.startswith("#")

    # Kısa özet olmalı, gereksiz uzun cümleler ve robotik ifadeler olmamalı
    assert len(post["description"].split(".")) <= 3
    assert "yapay zeka" not in post["description"].lower()


def test_duygu_ai_instagram_post_contains_exactly_five_hashtags_and_no_emojis():
    cfg = {
        "project_name": PROJECT_DUYGU,
        "brand_name": "FitShaker Pro",
        "brief": "Sporcu protein shaker şişesi, sızdırmaz kapak, paslanmaz çelik",
        "content_type": "Ürün Reklamı",
        "scene_count": 1,
        "seconds_per_scene": 5,
        "video_format": "9:16",
        "gem_url": "https://gemini.google.com/gem/test",
    }
    job = create_job_order(cfg, [])
    job.scenes = [
        SceneItem(index=1, title="Ürün Tanıtımı", description="Şık siyah shaker yakından gösterilir"),
    ]
    post = generate_instagram_post(job, job.scenes)

    assert len(post["hashtags"]) == 5
    assert strip_emojis_and_icons(post["title"]) == post["title"]
    assert strip_emojis_and_icons(post["description"]) == post["description"]
    for tag in post["hashtags"]:
        assert strip_emojis_and_icons(tag) == tag
        assert tag.startswith("#")


def test_prepare_instagram_package_creates_files():
    temp_dir = Path(tempfile.mkdtemp())
    try:
        scenarios_dir = temp_dir / "01_Senaryo"
        images_dir = temp_dir / "02_Gorseller"
        videos_dir = temp_dir / "03_Videolar"
        scenarios_dir.mkdir(parents=True)
        images_dir.mkdir(parents=True)
        videos_dir.mkdir(parents=True)

        # Örnek sahne 1 görseli oluştur
        (images_dir / "sahne_1.png").write_bytes(b"dummy_png_bytes_with_some_length_over_1000" * 40)

        dirs = {
            "base": temp_dir,
            "scenarios": scenarios_dir,
            "images": images_dir,
            "videos": videos_dir,
        }
        cfg = {
            "project_name": PROJECT_IKIZLER,
            "brand_name": "İkizler Masal Durağı",
            "brief": "Masal Konusu: Cesur Karınca",
            "content_type": "Masal / Animasyon",
            "output_dir": str(temp_dir),
            "scene_count": 1,
            "seconds_per_scene": 5,
            "video_format": "9:16",
            "gem_url": "https://gemini.google.com/gem/test",
        }
        job = create_job_order(cfg, [])
        job.output_dir = str(temp_dir)
        job.scenes = [SceneItem(index=1, title="Cesur Karınca", description="Yola çıkar")]

        post = prepare_instagram_package(job, dirs)

        # instagram_paylasim.txt dosyaları kontrol
        txt_file = scenarios_dir / "instagram_paylasim.txt"
        assert txt_file.exists()
        content = txt_file.read_text(encoding="utf-8")
        assert "BAŞLIK:" in content
        assert "AÇIKLAMA:" in content
        assert "#okulöncesieğitim" in content
        assert "#egiticiiçerik" in content

        # Root dizindeki txt dosyasını da kontrol et
        assert (temp_dir / "instagram_paylasim.txt").exists()

        # Kapak fotoğrafı kontrol
        assert (images_dir / "kapak.png").exists()
        assert (videos_dir / "kapak.png").exists()
        assert (temp_dir / "kapak.png").exists()
        assert job.cover_image_path is not None
        assert job.instagram_post is not None
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_workflow_retries_on_non_limit_transient_error_after_30s():
    import tempfile
    import shutil
    import services.workflow as wf
    from unittest.mock import patch
    from providers.base import TransientGeminiError
    from services.workflow import run_content_workflow
    from tests.fakes import FakeProvider

    temp_dir = Path(tempfile.mkdtemp())
    old_outputs = wf.OUTPUTS_DIR
    old_jobs = wf.JOBS_DIR
    wf.OUTPUTS_DIR = temp_dir / "outputs"
    wf.JOBS_DIR = temp_dir / "jobs"
    try:
        cfg = {
            "gem_url": "https://gemini.google.com/gem/test",
            "target_folder_id": "mock_folder_123",
            "project_name": PROJECT_DUYGU,
            "brand_name": "TestMarka",
            "brief": "Harika ürün",
            "scene_count": 1,
            "seconds_per_scene": 5,
            "video_format": "9:16",
            "dry_run": False,
        }
        transient_err = TransientGeminiError("Bir şeyler ters gitti. Lütfen tekrar deneyin.")
        provider = FakeProvider(
            video_errors=[transient_err, None],
            new_videos=["https://example.invalid/success.mp4"],
        )
        sleep_calls = []

        def mock_sleep(sec):
            sleep_calls.append(sec)

        lines = []
        with patch("services.workflow.time.sleep", side_effect=mock_sleep):
            report = run_content_workflow(cfg, [], lines.append, provider=provider)

        assert 2 in sleep_calls
        assert report.job.scenes[0].status == "video_done"
        log_text = "\n".join(lines)
        assert "Gemini geçici hata uyarısı tespit edildi (limit hatası değil)" in log_text
        assert "Anında tekrar deneniyor" in log_text
    finally:
        wf.OUTPUTS_DIR = old_outputs
        wf.JOBS_DIR = old_jobs
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_workflow_does_not_retry_on_rate_limit_error():
    import tempfile
    import shutil
    import services.workflow as wf
    from unittest.mock import patch
    from providers.base import RateLimitError
    from services.workflow import run_content_workflow
    from tests.fakes import FakeProvider

    temp_dir = Path(tempfile.mkdtemp())
    old_outputs = wf.OUTPUTS_DIR
    old_jobs = wf.JOBS_DIR
    wf.OUTPUTS_DIR = temp_dir / "outputs"
    wf.JOBS_DIR = temp_dir / "jobs"
    try:
        cfg = {
            "gem_url": "https://gemini.google.com/gem/test",
            "target_folder_id": "mock_folder_123",
            "project_name": PROJECT_DUYGU,
            "brand_name": "TestMarka",
            "brief": "Harika ürün",
            "scene_count": 1,
            "seconds_per_scene": 5,
            "video_format": "9:16",
            "dry_run": False,
        }
        rate_err = RateLimitError("Kullanım sınırına ulaştınız.")
        provider = FakeProvider(video_errors=[rate_err])
        sleep_calls = []

        def mock_sleep(sec):
            sleep_calls.append(sec)

        lines = []
        with patch("services.workflow.time.sleep", side_effect=mock_sleep):
            report = run_content_workflow(cfg, [], lines.append, provider=provider)

        assert 30 not in sleep_calls
        assert report.job.scenes[0].status == "rate_limited"
        assert report.overall_status == "rate_limited"
        log_text = "\n".join(lines)
        assert "GEMINI LİMİTİNE TAKILDI" in log_text
    finally:
        wf.OUTPUTS_DIR = old_outputs
        wf.JOBS_DIR = old_jobs
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_gemini_encountering_error_detected_as_transient_error():
    import pytest
    from providers.gemini import GeminiWebProvider
    from providers.base import TransientGeminiError

    provider = GeminiWebProvider({"gem_url": "https://gemini.google.com/gem/test"})
    error_message = "I seem to be encountering an error. Can I try something else for you?"
    with pytest.raises(TransientGeminiError) as exc_info:
        provider.check_text_safety_or_limits(error_message)
    assert "Geçici hata" in str(exc_info.value) or "geçici hata" in str(exc_info.value)


def test_gemini_abort_all_marks_instances_aborted():
    from providers.gemini import GeminiWebProvider

    p1 = GeminiWebProvider({"gem_url": "https://gemini.google.com/gem/test1"})
    p2 = GeminiWebProvider({"gem_url": "https://gemini.google.com/gem/test2"})

    try:
        GeminiWebProvider._active_instances.add(p1)
        GeminiWebProvider._active_instances.add(p2)
        assert not p1.is_aborted
        assert not p2.is_aborted

        GeminiWebProvider.abort_all()
        assert p1.is_aborted
        assert p2.is_aborted
    finally:
        GeminiWebProvider._active_instances.discard(p1)
        GeminiWebProvider._active_instances.discard(p2)


def test_instagram_hashtags_never_contain_command_words():
    cfg = {
        "project_name": PROJECT_IKIZLER,
        "brief": "ikizlerle okul öncesi eğitici içerik olabilecek bir senaryo üret ve videosunu oluştur",
        "content_type": "Masal / Animasyon",
        "scene_count": 2,
        "seconds_per_scene": 5,
        "video_format": "9:16",
    }
    job = create_job_order(cfg, [])
    job.scenes = [
        SceneItem(index=1, title="Sahne 1: Renkleri Öğreniyoruz", description="Atakan ve Kumsal renkli blokları dizer"),
        SceneItem(index=2, title="Sahne 2: Başarı Kutlaması", description="İkizler sevinçle birbirine sarılır"),
    ]
    post = generate_instagram_post(job, job.scenes)

    for forbidden in ["#oluştur", "#üret", "#videosunu", "#senaryo", "#olabilecek", "#içerik"]:
        assert forbidden not in post["hashtags"], f"Yasaklı hashtag bulundu: {forbidden}"

    assert "#okulöncesieğitim" in post["hashtags"]
    assert "#egiticiiçerik" in post["hashtags"]
    assert len(post["hashtags"]) == 5
    assert "üret ve videosunu oluştur" not in post["description"]


def test_ask_gemini_for_instagram_package_generates_post_and_cover():
    from tests.fakes import FakeProvider
    from services.social_media import ask_gemini_for_instagram_package

    temp_dir = Path(tempfile.mkdtemp())
    try:
        scenarios_dir = temp_dir / "01_Senaryo"
        images_dir = temp_dir / "02_Gorseller"
        videos_dir = temp_dir / "03_Videolar"
        scenarios_dir.mkdir(parents=True)
        images_dir.mkdir(parents=True)
        videos_dir.mkdir(parents=True)

        dirs = {
            "base": temp_dir,
            "scenarios": scenarios_dir,
            "images": images_dir,
            "videos": videos_dir,
        }
        cfg = {
            "project_name": PROJECT_IKIZLER,
            "brief": "ikizlerle okul öncesi eğitici içerik",
            "content_type": "Masal / Animasyon",
            "scene_count": 2,
            "seconds_per_scene": 10,
            "video_format": "9:16",
            "dry_run": False,
        }
        job = create_job_order(cfg, [])
        job.scenes = [
            SceneItem(index=1, title="Giriş", description="Atakan ve Kumsal merakla bakar", voiceover_text="Merhaba arkadaşlar!"),
            SceneItem(index=2, title="Oyun", description="Birlikte blokları dizerler", voiceover_text="Bakın burada renkler var!"),
        ]

        gemini_response = (
            "BAŞLIK: Atakan ve Kumsal ile Renkleri Keşfediyoruz\n"
            "AÇIKLAMA: Sevimli ikizlerimiz bu neşeli macerada renkli bloklarla şekilleri keşfe çıkıyor. "
            "Okul öncesi minikler için hem öğretici hem de neşe dolu sıcacık bir oyun saati.\n"
            "HASHTAGLER: #okulöncesieğitim #egiticiiçerik #çocukmasalları #okulöncesietkinlik #eğiticioyunlar\n"
            "KAPAK_PROMPT: 3D cartoon animation Pixar style: Cheerful toddler twins Atakan and Kumsal smiling happily together while building a colorful castle with wooden blocks, soft warm studio lighting, 9:16 vertical orientation\n"
        )
        provider = FakeProvider(response_text=gemini_response)

        post = ask_gemini_for_instagram_package(provider, job, dirs)
        assert post is not None
        assert post["title"] == "Atakan ve Kumsal ile Renkleri Keşfediyoruz"
        assert "#okulöncesieğitim" in post["hashtags"]
        assert "#egiticiiçerik" in post["hashtags"]
        assert len(post["hashtags"]) == 5

        # Kapak fotoğrafının üretildiğini doğrula
        assert (images_dir / "kapak.png").exists()
        assert (videos_dir / "kapak.png").exists()
        assert (temp_dir / "kapak.png").exists()
        assert job.cover_image_path == str(images_dir / "kapak.png")

        # Provider çağrılarını doğrula: Hem metin promptu hem de kapak görsel promptu gönderilmeli
        assert "send_prompt" in provider.calls
        assert any("Create an image:" in p for p in provider.sent_prompts)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_parse_scene_breakdown_faithfully_preserves_gemini_output():
    from services.prompt_builder import parse_scene_breakdown

    text_with_gemini_vo = (
        "[SAHNE 1]\nBAŞLIK: Meraklı Bakış\nAÇIKLAMA: Çocuklar odaya girer\n"
        "GÖRSEL_PROMPT: 3D cartoon animation Pixar style: kids in room, 9:16 vertical orientation\n"
        "VIDEO_PROMPT: 3D cartoon animation Pixar style: kids smile, 9:16 vertical video\n"
        "SESLENDIRME: Merhaba minikler, bugün harika bir oyun keşfediyoruz!\n\n"
        "[SAHNE 2]\nBAŞLIK: Neşeli Oyun\nAÇIKLAMA: Blokları üst üste koyarlar\n"
        "GÖRSEL_PROMPT: 3D cartoon animation Pixar style: playing with blocks, 9:16 vertical orientation\n"
        "VIDEO_PROMPT: 3D cartoon animation Pixar style: dynamic motion, 9:16 vertical video\n"
        "SESLENDIRME:\n"
    )

    scenes = parse_scene_breakdown(text_with_gemini_vo, default_count=2, default_seconds=10, is_animation=True)
    assert len(scenes) == 2
    # Gemini'nin ürettiği seslendirme birebir korunmalı
    assert scenes[0].voiceover_text == "Merhaba minikler, bugün harika bir oyun keşfediyoruz!"
    # Gemini seslendirme üretmediyse kod elle diyalog uydurmamalı, Gemini çıktısı olduğu gibi kalmalı
    assert scenes[1].voiceover_text == ""


def test_parse_scene_breakdown_user_anger_scenario():
    from services.prompt_builder import parse_scene_breakdown

    raw_gemini = (
        "[SAHNE 1]\n"
        "BAŞLIK: Öfkeli An\n"
        "AÇIKLAMA: Atakan kulesi yıkıldığı için öfkelenir ve kollarını kavuşturur. Atakan'ın üzerinde dinozor baskılı bej tişörtü, yeşil şortu ve beyaz ayakkabıları bulunmaktadır.\n"
        "GÖRSEL_PROMPT: 3D cartoon animation, Pixar style. A cute 4-year-old boy named Atakan with dark curly hair and big brown eyes, wearing a light beige t-shirt with a dinosaur print, green shorts, and white sneakers, is standing in a brightly lit, cozy playroom with his arms crossed, looking angry and frustrated next to fallen colorful toy blocks. Soft volumetric studio lighting, vibrant rich colors, highly detailed CGI render, 9:16 vertical.\n"
        "VIDEO_PROMPT: 3D cartoon animation, Pixar style motion. The angry boy stomps his foot lightly and crosses his arms tighter, his face showing frustration. The camera slowly zooms in on his expressive face. Smooth expressive character movement, dynamic cinematic lighting, lively physics, 9:16 vertical video.\n"
        "SESLENDIRME: Anlatıcı: Atakan'ın kulesi devrilince içine kocaman bir öfke bulutu yerleşti. Bazen işler ters gittiğinde hepimiz böyle hissedebiliriz.   [SAHNE 2]\n"
        "BAŞLIK: Derin Nefes ile Sakinleşme\n"
        "AÇIKLAMA: Pembe tüllü elbisesi, pembe ayakkabıları ve saçındaki pembe tokasıyla Kumsal, Atakan'a sakinleşmesi için derin bir nefes almayı gösterir. Atakan da onu taklit ederek rahatlar.\n"
        "GÖRSEL_PROMPT: 3D cartoon animation, Pixar style. A cute 4-year-old girl named Kumsal with brown hair tied up with a pink bow, big brown eyes, wearing a sparkly pink tulle dress and pink shoes, is smiling gently and demonstrating taking a deep breath to Atakan in the bright playroom. Atakan is watching her, uncrossing his arms and looking more relaxed. Soft volumetric studio lighting, cheerful warm colors, highly detailed CGI render, 9:16 vertical.\n"
        "VIDEO_PROMPT: 3D cartoon animation, Pixar style motion. The girl breathes in deeply, lifting her hands up slowly, and breathes out with a soft, comforting smile. The boy copies her movements, drops his shoulders, and smiles back, looking completely relaxed. Smooth expressive character movement, lively cinematic physics, gentle camera pan, 9:16 vertical video.\n"
        "SESLENDIRME: Kumsal: Atakan, hadi benim gibi derin bir nefes al ve üfle... Ffffuuu! Bak, öfke bulutu uçup gitti bile!"
    )

    scenes = parse_scene_breakdown(raw_gemini, default_count=2, default_seconds=10, is_animation=True)
    assert len(scenes) == 2

    # Sahne 1 kontrolleri
    assert scenes[0].index == 1
    assert scenes[0].title == "Öfkeli An"
    assert "Atakan kulesi yıkıldığı için öfkelenir" in scenes[0].description
    assert "dinozor baskılı bej tişörtü" in scenes[0].description
    assert "dinosaur print" in scenes[0].image_prompt
    assert "angry boy stomps his foot" in scenes[0].video_prompt
    assert scenes[0].voiceover_text == "Anlatıcı: Atakan'ın kulesi devrilince içine kocaman bir öfke bulutu yerleşti. Bazen işler ters gittiğinde hepimiz böyle hissedebiliriz."

    # Sahne 2 kontrolleri
    assert scenes[1].index == 2
    assert scenes[1].title == "Derin Nefes ile Sakinleşme"
    assert "Kumsal, Atakan'a sakinleşmesi için" in scenes[1].description
    assert "sparkly pink tulle dress" in scenes[1].image_prompt
    assert "The girl breathes in deeply" in scenes[1].video_prompt
    assert scenes[1].voiceover_text == "Kumsal: Atakan, hadi benim gibi derin bir nefes al ve üfle... Ffffuuu! Bak, öfke bulutu uçup gitti bile!"
