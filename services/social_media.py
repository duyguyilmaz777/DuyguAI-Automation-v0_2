from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from models.job import PROJECT_DUYGU, PROJECT_IKIZLER, JobOrder, SceneItem
from services.prompt_builder import sanitize_topic_for_prompts
from services.video_composer import find_ffmpeg


# Emoji ve özel simge temizleme deseni
EMOJI_PATTERN = re.compile(
    r"[\U00010000-\U0010ffff"  # Ek semboller, emojiler
    r"\u2600-\u26ff"          # Çeşitli semboller
    r"\u2700-\u27bf"          # Dingbat simgeleri
    r"\ufe00-\ufe0f"          # Varyasyon seçiciler
    r"\u200d"                 # Sıfır genişlikli birleştirici
    r"\u2300-\u23ff"          # Teknik simgeler
    r"\u2b50-\u2b55"          # Yıldız ve geometrik simgeler
    r"]+",
    flags=re.UNICODE,
)

# Hashtag ve konu kelimelerinde kesinlikle elenecek durdurma ve süreç kelimeleri
STOP_WORDS = {
    "ve", "ile", "için", "gibi", "bir", "bu", "şu", "da", "de", "düz", "icin", "en", "çok",
    "daha", "kadar", "olan", "tüm", "her", "ama", "fakat", "ya", "yaş", "grubu", "konusu",
    "masal", "hedef", "ana", "karakterler", "sahne", "video", "brief", "not", "isteğe", "bağlı",
    "oluştur", "üret", "videosunu", "senaryo", "olabilecek", "içerik", "hazırla", "yaz", "et",
    "yap", "çek", "tanıt", "iste", "istiyorum", "üretimi", "oluşturulsun", "yapılsın", "görsel",
    "görseller", "görseli", "animasyon", "dakika", "saniye", "bölüm", "tür", "özel", "reklam",
    "birebir", "türkçe", "seslendirme", "kısa", "uzun", "tarafından", "şekilde", "olarak",
    "kullanıcı", "otomasyon", "prompt", "cevap", "cevapla", "örnek", "örneğin", "istenen",
    "yapay", "zeka", "modeli", "programlanmadım", "yardımcı",
}


def strip_emojis_and_icons(text: str) -> str:
    """Metindeki tüm emojileri, sembolleri ve süslemeleri temizler."""
    if not text:
        return ""
    cleaned = EMOJI_PATTERN.sub("", text)
    # Birden fazla boşluğu teke indir
    cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
    return cleaned


def clean_short_summary(text: str, max_sentences: int = 2) -> str:
    """Metni AI kokmayan, kısa ve akıcı 1-2 cümleye dönüştürür.
    Uzun satırları ve cümleleri eler, ikonları temizler.
    """
    cleaned = strip_emojis_and_icons(text)
    cleaned = re.sub(r"[\r\n]+", " ", cleaned).strip()

    # AI ve robotik kalıpları temizle
    ai_phrases = [
        r"(?i)bu (video|içerik|görsel|reklam|masal) (yapay zeka|ai|gemini) (ile|tarafından) üretilmiştir",
        r"(?i)merhaba arkadaşlar",
        r"(?i)gözlerinize inanamayacaksınız",
        r"(?i)işte o muhteşem an",
        r"(?i)videomuzu beğenmeyi unutmayın",
        r"(?i)abone olmayı unutmayın",
        r"(?i)kaydetmeyi unutmayın",
    ]
    for pat in ai_phrases:
        cleaned = re.sub(pat, "", cleaned).strip()

    # Cümlelere böl
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", cleaned) if s.strip()]
    if not sentences:
        return cleaned

    selected: list[str] = []
    for s in sentences:
        words = s.split()
        if len(words) > 28:
            s = " ".join(words[:24]) + "..."
        if s and not s.startswith("#"):
            selected.append(s)
        if len(selected) >= max_sentences:
            break

    result = " ".join(selected)
    if result and result[-1] not in ".!?":
        result += "."
    return result


def _extract_topic_words(text: str) -> list[str]:
    cleaned = strip_emojis_and_icons(text).lower()
    words = re.findall(r"[a-zA-ZçğıöşüÇĞİÖŞÜ0-9]+", cleaned)
    return [w for w in words if len(w) > 3 and w not in STOP_WORDS]


def build_instagram_prompt(job: JobOrder, scenes: list[SceneItem] | None = None) -> str:
    """Gemini'ye gönderilerek video ve senaryoya uygun Instagram başlık, açıklama,
    5 hashtag ve kapak fotoğrafı promptu isteyen detaylı prompt hazırlar.
    """
    is_ikizler = (
        getattr(job, "project_name", "") == PROJECT_IKIZLER
        or "masal" in (job.content_type or "").lower()
    )

    scene_lines: list[str] = []
    if scenes:
        for s in scenes:
            scene_lines.append(f"- Sahne {s.index}: {s.title} | {s.description}")
            if s.voiceover_text:
                scene_lines.append(f"  Seslendirme: {s.voiceover_text}")
    scene_summary = "\n".join(scene_lines) if scene_lines else (job.scenario or job.brief or "Eğlenceli video")

    if is_ikizler:
        role_desc = (
            "Sen çocuk gelişimi ve okul öncesi içeriklerinde uzman, yaratıcı bir sosyal medya yöneticisisin. "
            "Az önce prodüksiyonu tamamlanan okul öncesi eğitici animasyon/masal videosu için Instagram Reels / Video "
            "paylaşım paketi ve kapak fotoğrafı promptu hazırla."
        )
        tag_rule = (
            "HASHTAGLER: Konuyla doğrudan ilgili TAM OLARAK 5 adet hashtag öner. "
            "İlk iki hashtag zorunlu olarak #okulöncesieğitim ve #egiticiiçerik olmalıdır. "
            "Kalan 3 hashtag ise senaryodaki konuya, karakterlere veya pedagojik temaya uygun olmalıdır. "
            "ASLA #üret, #oluştur, #videosunu, #senaryo gibi komut ve süreç kelimeleri KULLANMA!"
        )
        kapak_guide = (
            "3D cartoon animation Pixar style: Karakterlerin (Atakan ve Kumsal) en neşeli, meraklı veya tebessüm eden anını "
            "canlı renkler, sevimli mimikler, yumuşak stüdyo aydınlatması ve 9:16 vertical orientation ile anlatan İngilizce tek cümle."
        )
    else:
        role_desc = (
            "Sen premium markalar için çalışan kıdemli bir sosyal medya ve dijital pazarlama uzmanısın. "
            "Az önce üretilen ticari ürün tanıtım videosu için Instagram Reels / Video paylaşım paketi ve kapak görseli promptu hazırla."
        )
        tag_rule = (
            "HASHTAGLER: Ürün ve yaşam tarzıyla ilgili TAM OLARAK 5 adet popüler ve etkileşim getiren hashtag öner. "
            "ASLA #üret, #oluştur, #videosunu, #senaryo gibi komut ve süreç kelimeleri KULLANMA!"
        )
        kapak_guide = (
            "Commercial product photography: Ürünün en estetik, dikkat çekici ve premium sunumunu "
            "temiz arka plan, lüks aydınlatma, 8k netlik ve 9:16 vertical orientation ile anlatan İngilizce tek cümle."
        )

    return f"""{role_desc}

Video Bilgileri:
Proje: {job.project_name or ('İkizler Masal' if is_ikizler else 'DuyguAI')}
İçerik Türü: {job.content_type}
Senaryo / Sahne Akışı:
{scene_summary}

GÖREV:
Aşağıdaki 4 alanı tam ve eksiksiz doldur. AI tarafından yazıldığı anlaşılmayacak, samimi, akıcı ve profesyonel olmalıdır.
1. BAŞLIK: Video içeriğini ve ana fikrini yansıtan kısa, vurucu başlık (En fazla 5-7 kelime, KESİNLİKLE emoji veya sembol İÇERMESİN).
2. AÇIKLAMA: Videonun konusunu özetleyen, izleyiciyi içine çeken 1-2 cümlelik doğal açıklama (KESİNLİKLE emoji veya sembol İÇERMESİN, kullanıcı briefindeki emir kiplerini tekrarlama).
3. {tag_rule}
4. KAPAK_PROMPT: Videonun Instagram kapak fotoğrafını (cover photo) Gemini görsel üretim motoruna çizdirmek için tek cümlelik detaylı İngilizce görsel promptu ({kapak_guide}).

Çıktıyı tam olarak aşağıdaki formatta ver (başka metin ekleme):
BAŞLIK: (başlık buraya)
AÇIKLAMA: (açıklama buraya)
HASHTAGLER: (#tag1 #tag2 #tag3 #tag4 #tag5)
KAPAK_PROMPT: (İngilizce prompt buraya)
"""


def ask_gemini_for_instagram_package(
    provider: Any,
    job: JobOrder,
    dirs: dict[str, Path],
    log: Callable[[str], None] | None = None,
) -> dict[str, Any] | None:
    """Gemini'ye video içeriğine uygun Instagram metni ve kapak görseli promptu sorar,
    ardından Gemini'ye kapak görselini ürettirir.
    """
    if not provider or getattr(job, "dry_run", False):
        return None
    if not hasattr(provider, "send_prompt") or not hasattr(provider, "wait_for_new_response_text"):
        return None

    is_ikizler = (
        getattr(job, "project_name", "") == PROJECT_IKIZLER
        or "masal" in (job.content_type or "").lower()
    )

    if log:
        log("📱 Gemini'den senaryoya uygun Instagram paylaşım paketi ve kapak promptu isteniyor…")

    ig_prompt = build_instagram_prompt(job, job.scenes)
    resp_text = None
    try:
        provider.send_prompt(ig_prompt)
        resp_text = provider.wait_for_new_response_text(timeout_seconds=45)
    except Exception as e:
        if log:
            log(f"Gemini Instagram metni üretim uyarısı: {e}")
        return None

    if not resp_text or len(resp_text.strip()) < 15:
        return None

    def _field(prefixes: list[str]) -> str:
        for p in prefixes:
            m = re.search(rf"(?i){re.escape(p)}\s*[:\-]\s*(.*?)(?=\n[A-ZÇĞİÖŞÜ_]{{2,}}|\Z)", resp_text, re.DOTALL)
            if m:
                return m.group(1).strip()
        return ""

    title = _field(["BAŞLIK", "BASLIK", "TITLE"])
    desc = _field(["AÇIKLAMA", "ACIKLAMA", "DESCRIPTION"])
    tags_raw = _field(["HASHTAGLER", "HASHTAGS", "ETİKETLER", "TAGS"])
    cover_prompt = _field(["KAPAK_PROMPT", "COVER_PROMPT", "KAPAK PROMPT", "COVER"])

    title = clean_short_summary(title, max_sentences=1).rstrip(".")
    title = strip_emojis_and_icons(title)
    if not title or len(title.split()) > 8:
        title = " ".join(title.split()[:7])

    desc = clean_short_summary(desc, max_sentences=2)
    desc = strip_emojis_and_icons(desc)

    # Hashtag ayrıştırma ve filtreleme
    raw_tags = [t.lower() for t in re.findall(r"#[a-zA-ZçğıöşüÇĞİÖŞÜ0-9]+", tags_raw)]
    clean_tags: list[str] = []
    for t in raw_tags:
        core = t.lstrip("#")
        if core not in STOP_WORDS and len(core) >= 3 and t not in clean_tags:
            clean_tags.append(t)

    if is_ikizler:
        for mand in ["#okulöncesieğitim", "#egiticiiçerik"]:
            if mand not in clean_tags:
                clean_tags.insert(0, mand)

    clean_tags = clean_tags[:5]
    fallback_pool = (
        ["#okulöncesieğitim", "#egiticiiçerik", "#çocukmasalları", "#eğiticioyunlar", "#okulöncesietkinlik"]
        if is_ikizler else
        ["#ürüninceleme", "#kaliteliürün", "#pratikürünler", "#reelsürün", "#markatanıtımı"]
    )
    for fb in fallback_pool:
        if len(clean_tags) >= 5:
            break
        if fb not in clean_tags:
            clean_tags.append(fb)

    post_data = {
        "title": title or ("Sevimli Masal Saati" if is_ikizler else "Günün Özel Ürünü"),
        "description": desc or ("Okul öncesi yaş grubu için keyifli ve öğretici bir içerik." if is_ikizler else "Modern detayları ve kaliteli tasarımıyla her an yanınızda."),
        "hashtags": clean_tags[:5],
        "cover_prompt": cover_prompt,
    }
    post_data["formatted_post"] = f"{post_data['title']}\n\n{post_data['description']}\n\n{' '.join(post_data['hashtags'])}"

    # Kapak Görselini Gemini'ye Çizdir
    images_dir = dirs.get("images")
    if cover_prompt and len(cover_prompt.strip()) > 10 and images_dir:
        if log:
            log(f"🎨 Gemini'den senaryoya uygun özel kapak görseli çizimi isteniyor...")
        clean_cp = sanitize_topic_for_prompts(cover_prompt)
        if "9:16" not in clean_cp:
            clean_cp = f"{clean_cp}, 9:16 vertical orientation"
        img_gen_prompt = f"Create an image: {clean_cp}."
        try:
            init_count = provider.get_image_count() if hasattr(provider, "get_image_count") else 0
            old_sources = provider.snapshot_images() if hasattr(provider, "snapshot_images") else set()
            provider.send_prompt(img_gen_prompt)
            new_img = None
            if hasattr(provider, "wait_for_new_image"):
                new_img = provider.wait_for_new_image(timeout_minutes=3, old_sources=old_sources, initial_count=init_count)

            target_cover = images_dir / "kapak.png"
            saved = False
            if hasattr(provider, "save_latest_generated_image"):
                saved = provider.save_latest_generated_image(target_cover)
            elif new_img and hasattr(provider, "download_file"):
                saved = provider.download_file(new_img, target_cover)

            if saved and target_cover.exists() and target_cover.stat().st_size > 20:
                if dirs.get("videos"):
                    shutil.copyfile(target_cover, dirs["videos"] / "kapak.png")
                if dirs.get("base"):
                    shutil.copyfile(target_cover, dirs["base"] / "kapak.png")
                post_data["cover_image"] = str(target_cover)
                job.cover_image_path = str(target_cover)
                if log:
                    log(f"🖼️ Gemini tarafından özel kapak görseli başarıyla üretildi: kapak.png")
        except Exception as img_err:
            if log:
                log(f"Gemini kapak görseli çizimi uyarısı: {img_err}")

    return post_data


def generate_instagram_post(
    job: JobOrder,
    scenes: list[SceneItem] | None = None,
) -> dict[str, Any]:
    """Her iki proje için Instagram'da yayınlanacak başlık, kısa özet açıklama
    ve tam olarak 5 hashtag önerisi üretir. AI kokmayan, doğal ve ikonsuzdur.
    """
    is_ikizler = (
        getattr(job, "project_name", "") == PROJECT_IKIZLER
        or "masal" in (job.content_type or "").lower()
    )

    brief_raw = getattr(job, "brief", "") or ""
    scenario_raw = getattr(job, "scenario", "") or ""
    extra_raw = getattr(job, "extra_notes", "") or ""
    brand_raw = getattr(job, "brand_name", "") or ""

    # Başlık oluşturma
    title = ""
    if is_ikizler:
        m_topic = re.search(r"(?i)masal konusu\s*:\s*([^\n\r]+)", brief_raw)
        if m_topic:
            title = m_topic.group(1).strip()
        elif scenes and scenes[0].title:
            clean_t = re.sub(r"(?i)^sahne\s*\d+\s*[:\-]\s*", "", scenes[0].title).strip()
            title = clean_t
        else:
            words = _extract_topic_words(f"{brief_raw} {brand_raw}")
            if words:
                title = " ".join(w.capitalize() for w in words[:4])
            else:
                title = "Atakan ve Kumsal ile Masal Saati"
    else:
        if brand_raw and brand_raw not in ("DuyguAI", "DuyguAI_Kampanya"):
            clean_brand = brand_raw.replace("_", " ").strip()
            title = f"{clean_brand} ile Yeni Deneyim"
        elif scenes and scenes[0].title:
            clean_t = re.sub(r"(?i)^sahne\s*\d+\s*[:\-]\s*", "", scenes[0].title).strip()
            title = clean_t
        else:
            words = _extract_topic_words(brief_raw)
            if words:
                title = " ".join(w.capitalize() for w in words[:4])
            else:
                title = "Günün Öne Çıkan Ürünü"

    title = strip_emojis_and_icons(title)
    t_words = title.split()
    if len(t_words) > 7:
        title = " ".join(t_words[:6])

    # Açıklama oluşturma
    description = ""
    if is_ikizler:
        m_goal = re.search(r"(?i)eğitsel hedef\s*:\s*([^\n\r]+)", brief_raw)
        m_chars = re.search(r"(?i)karakterler\s*:\s*([^\n\r]+)", brief_raw)
        m_topic = re.search(r"(?i)masal konusu\s*:\s*([^\n\r]+)", brief_raw)

        c_text = m_chars.group(1).strip() if m_chars else ""
        g_text = m_goal.group(1).strip() if m_goal else ""
        t_text = m_topic.group(1).strip() if m_topic else ""

        if c_text and g_text:
            first_sentence = f"{c_text} bu macerada {g_text.lower()} konusunu keşfe çıkıyor."
        elif t_text and g_text:
            first_sentence = f"{t_text} temalı masalımızda {g_text.lower()} üzerine sıcacık bir hikaye yer alıyor."
        elif t_text:
            first_sentence = f"{t_text} eşliğinde çocukların hayal dünyasını zenginleştiren keyifli bir anlatım."
        elif scenes and scenes[0].description and len(scenes[0].description) > 15:
            first_sentence = clean_short_summary(scenes[0].description, max_sentences=1)
        else:
            first_sentence = "Atakan ve Kumsal ile çocukların hayal dünyasını zenginleştiren sıcacık bir masal."

        second_sentence = "Okul öncesi yaş grubu için hazırlanan eğlenceli ve öğretici bir içerik."
        description = f"{first_sentence} {second_sentence}"
    else:
        clean_brief = clean_short_summary(scenario_raw or extra_raw or brief_raw, max_sentences=2)
        if clean_brief and len(clean_brief.split()) >= 8 and not any(w in clean_brief.lower() for w in ["oluştur", "üret", "videosunu"]):
            description = clean_brief
        else:
            c_name = brand_raw.replace("_", " ") if brand_raw and brand_raw != "DuyguAI_Kampanya" else "Özel tasarım"
            description = (
                f"{c_name} ile günlük rutininizde pratiklik ve konforu bir arada yakalayın. "
                "Modern detayları ve kaliteli yapısıyla her an yanınızda."
            )

    description = clean_short_summary(description, max_sentences=2)

    # 5 Adet Hashtag Önerisi
    hashtags: list[str] = []
    if is_ikizler:
        mandatory = ["#okulöncesieğitim", "#egiticiiçerik"]
        hashtags.extend(mandatory)

        # Sahne başlıkları ve konusundan özel kelimeler çıkar (brief emirlerini değil)
        scene_text = " ".join([s.title for s in (scenes or [])])
        words = _extract_topic_words(f"{title} {scene_text}")
        candidate_pool = [
            "#çocukmasalları",
            "#masalsaati",
            "#çocukgelişimi",
            "#pedagojikiçerik",
            "#eğiticimasallar",
            "#çocukkitapları",
            "#eğiticioyunlar",
            "#okulöncesietkinlik",
        ]
        for w in words:
            tag = f"#{w.lower()}"
            if len(tag) > 3 and tag not in hashtags and tag not in candidate_pool:
                candidate_pool.insert(0, tag)

        for tag in candidate_pool:
            if tag not in hashtags:
                hashtags.append(tag)
            if len(hashtags) == 5:
                break
    else:
        scene_text = " ".join([s.title for s in (scenes or [])])
        words = _extract_topic_words(f"{title} {scene_text}")
        base_pool = [
            "#ürüninceleme",
            "#kaliteliürün",
            "#pratikürünler",
            "#sağlıklıyaşam",
            "#günlükkullanım",
            "#reelsürün",
            "#markatanıtımı",
        ]
        if "reklam" in (job.content_type or "").lower():
            base_pool.insert(0, "#ürünreklamı")
        if "spor" in (brief_raw or "").lower() or "shaker" in (brief_raw or "").lower():
            base_pool.insert(0, "#spormalzemeleri")
            base_pool.insert(1, "#fitnessmotivation")

        for w in words:
            tag = f"#{w.lower()}"
            if len(tag) > 3 and tag not in hashtags:
                hashtags.append(tag)
            if len(hashtags) == 5:
                break

        for tag in base_pool:
            if tag not in hashtags:
                hashtags.append(tag)
            if len(hashtags) == 5:
                break

    # Tam olarak 5 hashtag olmasını garanti et
    hashtags = hashtags[:5]
    while len(hashtags) < 5:
        fallback_tag = "#eğiticioyunlar" if is_ikizler else "#kaliteliürün"
        if fallback_tag not in hashtags:
            hashtags.append(fallback_tag)
        else:
            hashtags.append(f"#trend{len(hashtags)+1}")

    formatted_text = f"{title}\n\n{description}\n\n{' '.join(hashtags)}"
    return {
        "title": title,
        "description": description,
        "hashtags": hashtags,
        "formatted_post": formatted_text,
    }


def extract_or_create_cover_image(
    dirs: dict[str, Path],
    video_path: Path | str | None = None,
    log: Callable[[str], None] | None = None,
) -> Path | None:
    """Video ve sahne görsellerine uygun kapak fotoğrafı oluşturur.
    Öncelikle FFmpeg ile birleştirilmiş tam_video'dan 1. saniyedeki net kareyi çıkarır.
    Video mevcut değilse veya ffmpeg yoksa 1. sahne görselini kapak olarak kopyalar.
    Kapak fotoğrafını 02_Gorseller/kapak.png ve 03_Videolar/kapak.png konumlarına kaydeder.
    """
    images_dir = dirs.get("images")
    videos_dir = dirs.get("videos")
    base_dir = dirs.get("base")

    target_cover_img = images_dir / "kapak.png" if images_dir else None
    target_cover_vid = videos_dir / "kapak.png" if videos_dir else None
    target_cover_base = base_dir / "kapak.png" if base_dir else None

    # 1. Öncelik: Videodan 1. saniyedeki kareyi çıkarma (FFmpeg)
    vid_file = Path(video_path) if video_path else (videos_dir / "tam_video.mp4" if videos_dir else None)
    if vid_file and vid_file.is_file() and vid_file.stat().st_size > 1000:
        ffmpeg_bin = find_ffmpeg()
        if ffmpeg_bin and target_cover_img:
            try:
                import subprocess

                cmd = [
                    ffmpeg_bin,
                    "-y",
                    "-ss",
                    "00:00:01",
                    "-i",
                    str(vid_file.resolve()),
                    "-vframes",
                    "1",
                    "-q:v",
                    "2",
                    str(target_cover_img.resolve()),
                ]
                res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if res.returncode == 0 and target_cover_img.exists() and target_cover_img.stat().st_size > 500:
                    if log:
                        log(f"🖼️ Videodan net kapak fotoğrafı karesi üretildi: {target_cover_img.name}")
                    if target_cover_vid:
                        shutil.copyfile(target_cover_img, target_cover_vid)
                    if target_cover_base:
                        shutil.copyfile(target_cover_img, target_cover_base)
                    return target_cover_img
            except Exception as e:
                if log:
                    log(f"Videodan kapak karesi çıkarma notu: {e}")

    # 2. Öncelik: Mevcut 1. sahne görselini kapak yap
    candidate_img = None
    if images_dir and images_dir.is_dir():
        for i in range(1, 20):
            p = images_dir / f"sahne_{i}.png"
            if p.is_file() and p.stat().st_size > 1000:
                candidate_img = p
                break
        if not candidate_img:
            for p in sorted(images_dir.glob("*.png")):
                if p.is_file() and p.name != "kapak.png" and p.stat().st_size > 1000:
                    candidate_img = p
                    break

    if candidate_img and target_cover_img:
        try:
            shutil.copyfile(candidate_img, target_cover_img)
            if target_cover_vid:
                shutil.copyfile(target_cover_img, target_cover_vid)
            if target_cover_base:
                shutil.copyfile(target_cover_img, target_cover_base)
            if log:
                log(f"🖼️ Sahne görselinden kapak fotoğrafı hazırlandı: {candidate_img.name} -> kapak.png")
            return target_cover_img
        except Exception as e:
            if log:
                log(f"Kapak fotoğrafı kopyalama uyarısı: {e}")

    return target_cover_img if target_cover_img and target_cover_img.exists() else None


def prepare_instagram_package(
    job: JobOrder,
    dirs: dict[str, Path],
    log: Callable[[str], None] | None = None,
    provider: Any = None,
) -> dict[str, Any]:
    """Her iki proje için Instagram paylaşım metnini ve kapak fotoğrafını hazırlar,
    ilgili klasörlere kaydeder ve iş emrine ekler.
    Canlı modda önce Gemini'ye danışır ve kapak görselini Gemini ile üretir.
    """
    post_data = None
    if provider and not getattr(job, "dry_run", False):
        try:
            post_data = ask_gemini_for_instagram_package(provider, job, dirs, log)
        except Exception as e:
            if log:
                log(f"Gemini Instagram paketi uyarısı: {e}")

    if not post_data:
        post_data = generate_instagram_post(job, job.scenes)

    # Kapak fotoğrafı henüz oluşturulmadıysa (veya Gemini üretemediyse) fallback devreye girsin
    images_dir = dirs.get("images")
    has_cover = images_dir and (images_dir / "kapak.png").is_file() and (images_dir / "kapak.png").stat().st_size > 1000
    if not has_cover:
        merged_vid = (dirs.get("videos") / "tam_video.mp4") if dirs.get("videos") else None
        cover_path = extract_or_create_cover_image(dirs, video_path=merged_vid, log=log)
        if cover_path:
            post_data["cover_image"] = str(cover_path)
            job.cover_image_path = str(cover_path)
    elif images_dir and (images_dir / "kapak.png").is_file():
        post_data["cover_image"] = str(images_dir / "kapak.png")
        job.cover_image_path = str(images_dir / "kapak.png")

    job.instagram_post = post_data

    # 01_Senaryo ve proje kök dizinine instagram_paylasim.txt kaydet
    scenarios_dir = dirs.get("scenarios")
    base_dir = dirs.get("base")

    txt_content = (
        f"BAŞLIK:\n{post_data['title']}\n\n"
        f"AÇIKLAMA:\n{post_data['description']}\n\n"
        f"HASHTAGLER:\n{' '.join(post_data['hashtags'])}\n"
    )

    if scenarios_dir and scenarios_dir.is_dir():
        try:
            (scenarios_dir / "instagram_paylasim.txt").write_text(txt_content, encoding="utf-8")
        except Exception as e:
            if log:
                log(f"instagram_paylasim.txt kaydetme notu: {e}")

    if base_dir and base_dir.is_dir():
        try:
            (base_dir / "instagram_paylasim.txt").write_text(txt_content, encoding="utf-8")
        except Exception:
            pass

    if log:
        log("\n" + "=" * 50)
        log("📱 [INSTAGRAM PAYLAŞIM ÖNERİSİ & KAPAK HAZIRLANDI]")
        log(f"Başlık: {post_data['title']}")
        log(f"Açıklama: {post_data['description']}")
        log(f"Hashtagler: {' '.join(post_data['hashtags'])}")
        c_path = post_data.get("cover_image")
        if c_path:
            log(f"Kapak Fotoğrafı: {Path(c_path).name}")
        log("=" * 50)

    return post_data
