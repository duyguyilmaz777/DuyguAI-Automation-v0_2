from __future__ import annotations

import re
from pathlib import Path
from models.job import JobOrder, SceneItem


def is_animation_content_type(job: JobOrder | str | None) -> bool:
    """İçerik türünün animasyon veya masal olup olmadığını kontrol eder."""
    if not job:
        return False
    val = job.content_type if hasattr(job, "content_type") else str(job)
    proj = getattr(job, "project_name", "") if hasattr(job, "project_name") else ""
    return (
        "animasyon" in (val or "").strip().lower()
        or "masal" in (val or "").strip().lower()
        or "masal" in (proj or "").strip().lower()
    )


def sanitize_script_task_input(text: str) -> str:
    """Brief veya konu metnindeki 'video üret', 'videosunu yap' gibi dil modellerini
    'ben video üretemem' şeklinde ret yanıtına sevk eden komut kalıplarını temizler."""
    if not text:
        return ""
    cleaned = text
    patterns = [
        r"(?:ve\s+)?videosunu\s+üret(?:elim|iniz|sin)?",
        r"(?:ve\s+)?video(?:sunu)?\s+(?:üret(?:elim|iniz|sin)?|yap(?:alım|ınız|sın)?|çek(?:elim|iniz|sin)?|oluştur(?:alım|unuz|sun)?)",
        r"(?:ve\s+)?animasyonunu\s+(?:üret|yap|oluştur)",
        r"\bvideo\s+üret\b",
        r"\bvideo\s+yap\b",
        r"\bvideosunu\s+istiyorum\b",
        r"\bbana\s+video\s+ver\b",
    ]
    for pat in patterns:
        cleaned = re.sub(rf"(?i){pat}", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def build_script_breakdown_prompt(job: JobOrder, retry_attempt: int = 1) -> str:
    """Aşama 1: Gemini'den brief ve senaryoya göre sahne sahne prompt planı çıkarmasını ister.
    Gemini metin tabanlı bir model olduğu için video dosyası değil, metin senaryosu istendiği açıkça belirtilir.
    """
    clean_brief = sanitize_script_task_input(job.brief.strip()) or "(Brief belirtilmedi)"
    scenario_block = job.scenario.strip() or "(Hazır senaryo yok; brief'ten yaratıcı bir sahne akışı oluştur)"
    extra = job.extra_notes.strip() or "(Ek not yok)"
    is_anim = is_animation_content_type(job)

    if job.voiceover:
        voice = "Türkçe seslendirme işaretlendi. Sahnelere senaryoya uygun diyalog veya anlatım (dış ses) ekle."
        vo_desc = "(Senaryoya göre bu sahnede geçecek karakter diyaloğu veya anlatım metni)"
    else:
        voice = "Seslendirme zorunlu değil; senaryoya göre diyalog veya anlatım olabilir ya da boş bırakılabilir."
        vo_desc = "(Senaryoya göre isteğe bağlı diyalog/anlatım metni veya boş)"

    if is_anim:
        ref_note = (
            "Müşteri karakter/masal referans görsel(ler)i yükledi. Sahnelerdeki masal kahramanları, ana karakterler ve ortam detayları birebir bu referans görsellere sadık kalmalı, karakter tasarımı, yüz hatları, renkleri ve giysileri 3D Pixar animasyon stiline uyarlanarak kesinlikle korunmalıdır."
            if job.reference_images
            else "Referans görsel yok; masal konusu ve brief'e göre sıfırdan tamamen özgün, sevimli 3D Pixar tarzı karakterler tasarla."
        )
        img_prompt_desc = "(Bu sahnenin başlangıç karesi için 3D cartoon animation Pixar style, sevimli 3D karakter ve ortam detayları, yumuşak stüdyo ışığı, canlı renk paleti içeren detaylı İngilizce görsel üretim promptu.)"
        vid_prompt_desc = "(Bu görselden video üretmek için 3D cartoon animation Pixar style akıcı karakter hareketi, jestler ve mimikler içeren İngilizce kamera ve animasyon promptu. Örn: 3D cartoon animation, Pixar style motion, smooth expressive character movement, lively cinematic physics, 9:16 vertical video.)"
        anim_rule = (
            "3. ANİMASYON STİLİ (ZORUNLU KURAL): İçerik türü ANİMASYON seçilmiştir. Müşteri brief veya açıklamasında açıkça yazmasa bile; "
            "tüm sahnelerin görsel ve video dili kesinlikle '3D Cartoon Animation, Pixar Style' (Pixar ve Disney 3D animasyon tarzı sevimli 3D karakterler, "
            "canlı ve zengin renkler, yumuşak stüdyo/hacimsel aydınlatma, 3D CGI render kalitesi) olmalıdır. Her sahne için üretilecek GÖRSEL_PROMPT ve "
            "VIDEO_PROMPT'lar istisnasız bu 3D Pixar çizgi film animasyon tarzında yazılmalıdır.\n"
        )
    else:
        ref_note = (
            "Müşteri referans görsel(ler) yükledi. Sahnelerdeki ürün/karakter birebir bu referans görsellere sadık kalmalı, tasarımı veya detayları kesinlikle değiştirilmemelidir."
            if job.reference_images
            else "Referans görsel yok; brief'teki konsept ve ürüne göre sıfırdan tamamen özgün, modern ve yaratıcı sahneler tasarla."
        )
        img_prompt_desc = "(Bu sahnenin başlangıç karesi için fotogerçekçi, İngilizce görsel üretim promptu. Ürün detayları, stüdyo veya ortam, ışıklandırma, renk paleti ve kamera açısını detaylandır.)"
        vid_prompt_desc = "(Bu görselden video üretmek için bağımsız İngilizce kamera hareketi ve dinamizm promptu. Örn: Cinematic smooth camera movement, independent complete action, 9:16 vertical video, subtle rotation, premium lighting.)"
        anim_rule = ""

    # İkinci veya sonraki denemelerde modeli hiçbir guardrail'e takmadan doğrudan formata odaklayan yalın talimat
    if retry_attempt > 1:
        style_hint = "3D cartoon animation Pixar style" if is_anim else "Commercial product photography / cinematic"
        return f"""GÖREV: Sen profesyonel bir senaristsin. Bu aşamada senden doğrudan bir video dosyası veya görsel üretmen İSTENMEMEKTEDİR.
Yalnızca aşağıdaki konuya uygun olarak metin formatında sahne sahne senaryo ve görsel prompt planını yaz.

Konu: {clean_brief}
Sahne sayısı: {job.scene_count}
Stil: {style_hint}

Lütfen cevabında sohbet veya giriş cümlesi kurmadan DOĞRUDAN aşağıdaki [SAHNE 1] formatında sahne planını yaz:

[SAHNE 1]
BAŞLIK: (Kısa sahne adı)
AÇIKLAMA: (Sahnenin Türkçe açıklaması)
GÖRSEL_PROMPT: {img_prompt_desc}
VIDEO_PROMPT: {vid_prompt_desc}
SESLENDIRME: {vo_desc}

(Bu formatı tam olarak {job.scene_count} sahne için [SAHNE 1] .. [SAHNE {job.scene_count}] şeklinde tekrarla.)
"""

    return f"""GÖREV TALİMATI:
Sen profesyonel bir senarist ve görsel yönetmenisin.
ÖNEMLİ: Bu aşamada senden herhangi bir video dosyası veya görsel üretmen İSTENMEMEKTEDİR.
Yalnızca aşağıdaki müşteri talebi ve brief'e göre tam olarak {job.scene_count} sahnelik metin tabanlı prodüksiyon planı hazırlayacaksın.

İçerik türü: {job.content_type}
Hedef video formatı: {job.video_format}
Sahne sayısı: {job.scene_count}
Sahne başına süre: {job.seconds_per_scene} saniye
Seslendirme durumu: {voice}

Müşteri brief / açıklama:
{clean_brief}

Hazır senaryo (varsa sadık kal):
{scenario_block}

Ek kurgu / stil notları:
{extra}

Görsel Referans Durumu:
{ref_note}

ÖNEMLİ KURALLAR:
1. METİN TABANLI PLAN: Senden video dosyası değil, sadece sahnelerin metin dökümü ve üretim promptları istenmektedir.
2. BAĞIMSIZ SAHNELER: Her sahne kendi başına bağımsız bir çekim (standalone shot) olmalıdır.
3. GÖRSEL VE VİDEO PROMPTLARI: Her sahne için ayrıntılı İngilizce GÖRSEL_PROMPT ve bağımsız VIDEO_PROMPT yaz. GÖRSEL_PROMPT doğrudan görsel üretim modeline gideceği için meta-komut içermemeli, sahneyi doğrudan betimlemelidir.
{anim_rule}
GÖREV:
Her sahne için ayrıştırılmış İngilizce promptlar yaz. Çıktıyı tam olarak aşağıdaki blok formatında ver:

---
[SAHNE 1]
BAŞLIK: (Kısa sahne adı)
AÇIKLAMA: (Sahnenin kısa Türkçe özeti)
GÖRSEL_PROMPT: {img_prompt_desc}
VIDEO_PROMPT: {vid_prompt_desc}
SESLENDIRME: {vo_desc}
---
(Bu formatı {job.scene_count} sahne için tekrarla.)
"""


def sanitize_topic_for_prompts(text: str) -> str:
    """Brief veya metindeki meta komutları ve hassas ifadeleri temizler."""
    if not text:
        return ""
    cleaned = text
    meta_patterns = [
        r"ugc\s+içeriği\s+üret",
        r"ugc\s+içeriği",
        r"içeriği\s+üret",
        r"video\s+üret",
        r"görsel\s+üret",
        r"senaryo\s+yaz",
        r"senaryo\s+oluştur",
        r"tanıtabilir",
        r"avatar\s+karakter",
        r"avatar",
        r"dil\s+modeli",
        r"programlanmadım",
        r"yardımcı\s+olamıyorum",
        r"üsteğinizi\s+gerçekleştirebilecek",
    ]
    for pat in meta_patterns:
        cleaned = re.sub(rf"(?i)\b{pat}\b", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


# Gemini sahne bloklarındaki alan etiketleri (büyük harf, iki nokta ile biten)
_FIELD_LABELS_UPPER = [
    "BAŞLIK", "TITLE",
    "AÇIKLAMA", "DESCRIPTION",
    "GÖRSEL_PROMPT", "GÖRSEL PROMPT", "IMAGE_PROMPT", "IMAGE PROMPT",
    "VIDEO_PROMPT", "VİDEO_PROMPT", "VIDEO PROMPT", "VİDEO PROMPT",
    "SESLENDIRME", "SESLENDİRME", "VOICEOVER",
]
_LABEL_ALT = "|".join(re.escape(lbl) for lbl in sorted(_FIELD_LABELS_UPPER, key=len, reverse=True))

# Gemini arayüzünün yanıt metnine eklediği başlık/etiket satırları
_UI_NOISE_LINE_PATTERNS = [
    r"^\s*özel gem\s*$",
    r"^\s*custom gem\s*$",
    r"^.{0,120}\bşunu dedi\s*:?\s*$",
    r"^.{0,120}\bsaid\s*:?\s*$",
    r"^\s*(?:düşünme sürecini göster|düşünce sürecini göster|show thinking)\s*$",
]


def clean_model_response_text(text: str | None) -> str:
    """Gemini yanıtındaki arayüz kalıntılarını ("X şunu dedi:", "Özel Gem" vb.) temizler ve
    birbirine yapışık gelen alan etiketlerini (örn. "Başında!AÇIKLAMA:") ayrı satırlara böler.
    Eğer metin içinde [SAHNE 1] veya [SAHNE N] blokları varsa, öncesindeki model giriş/sohbet
    cümlelerini ayıklar ve doğrudan sahne planını döndürür.
    Gemini'nin ürettiği sahne içeriğinin kendisi değiştirilmez; yalnızca biçim düzenlenir.
    """
    if not text:
        return ""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")

    lines = cleaned.split("\n")
    # "X şunu dedi:" / "X said:" başlığı ilk satırlarda varsa, o satır dahil öncesini at (Gem adı vb.)
    for i, line in enumerate(lines[:8]):
        if re.match(r"^.{0,160}\b(?:şunu dedi|said)\s*:?\s*$", line, re.IGNORECASE):
            lines = lines[i + 1:]
            break
    # Yalnızca ilk sahne/metin başlamadan önceki arayüz satırlarını temizle
    out_lines: list[str] = []
    content_started = False
    for line in lines:
        if not content_started:
            if any(re.match(p, line, re.IGNORECASE) for p in _UI_NOISE_LINE_PATTERNS):
                continue
            if line.strip():
                content_started = True
        out_lines.append(line)
    cleaned = "\n".join(out_lines)

    # Yapışık sahne başlıklarını ayır: "...metin.[SAHNE 2]" -> "...metin.\n\n[SAHNE 2]"
    cleaned = re.sub(r"(?<!\n)[ \t]*(\[\s*(?:SAHNE|SCENE)\s*\d+[^\]]*\])", r"\n\n\1", cleaned)
    # Sahne başlığından sonra satır sonu yoksa ekle
    cleaned = re.sub(r"(\[\s*(?:SAHNE|SCENE)\s*\d+[^\]]*\])[ \t]*(?=\S)", r"\1\n", cleaned)
    # Yapışık alan etiketlerini ayır: "Başında!AÇIKLAMA:" -> "Başında!\nAÇIKLAMA:"
    cleaned = re.sub(
        rf"(?<!\n)[ \t]*(\**(?:{_LABEL_ALT})\**[ \t]*\**:)",
        r"\n\1",
        cleaned,
    )
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    # Eğer metin içinde sahne başlangıcı varsa ([SAHNE 1] veya [SAHNE N]),
    # öncesindeki sohbet ("Tabii ki...", "Ben metin tabanlı...") kısımlarını kırp
    first_scene_match = re.search(r"(?i)\[\s*(?:SAHNE|SCENE)\s*\d+[^\]]*\]", cleaned)
    if first_scene_match and first_scene_match.start() > 0:
        cleaned = cleaned[first_scene_match.start():].strip()

    return cleaned


def has_scene_markers(text: str | None) -> bool:
    """Metnin Gemini'nin ürettiği sahne bloklarını ([SAHNE N] + alan etiketleri) içerip içermediğini döner."""
    if not text:
        return False
    has_marker = re.search(r"(?i)\[\s*(?:SAHNE|SCENE)\s*\d+", text) is not None
    has_field = re.search(rf"(?:{_LABEL_ALT})\s*\**\s*:", text) is not None
    return has_marker and has_field


def clean_voiceover_for_prompt(vo: str) -> str:
    """Seslendirme metninden dosya adlarını (.png, .jpg vb.) ve gereksiz etiketleri temizler."""
    if not vo:
        return ""
    lines = []
    for line in vo.splitlines():
        line_s = line.strip()
        if re.search(r"\.(?:png|jpg|jpeg|webp|gif)\b", line_s, re.I):
            continue
        if line_s:
            lines.append(line_s)
    return " ".join(lines).strip()


def parse_scene_breakdown(
    text: str,
    default_count: int = 1,
    default_seconds: int = 5,
    fallback_prompt: str = "",
    is_animation: bool = False,
    job: JobOrder | None = None,
    voiceover: bool | None = None,
    include_dialogue_in_video_prompt: bool = False,
) -> list[SceneItem]:
    """Gemini'den gelen metin yanıtını ayrıştırıp SceneItem listesine çevirir.
    Gemini'nin ürettiği senaryo, başlık, promptlar ve diyaloglara asla kod ile müdahale edilmez;
    Gemini ne üretmişse doğrudan o kullanılır.
    """
    scenes: list[SceneItem] = []
    if not text or not text.strip():
        return _fallback_scenes(default_count, default_seconds, fallback_prompt, is_animation=is_animation)

    text = clean_model_response_text(text)

    # Öncelik: köşeli parantezli [SAHNE N] başlıkları (metin içindeki "Sahne 1'de" gibi ifadeler eşleşmesin)
    bracket_pattern = re.compile(r"(?i)\[\s*(?:SAHNE|SCENE)\s*(\d+)[^\]]*\]()")
    matches = list(bracket_pattern.finditer(text))
    if not matches:
        scene_pattern = re.compile(
            r"(?i)(?:\[\s*(?:SAHNE|SCENE)\s*(\d+)[^\]]*\]|(?:^|\n|\r|---|\*\*|#{1,4})\s*(?:SAHNE|SCENE)\s*(\d+)\b[^\n:]*(?::|\-|\n|\*\*))"
        )
        matches = list(scene_pattern.finditer(text))

    if not matches:
        extracted_img = _extract_field(text, ["GÖRSEL_PROMPT", "IMAGE_PROMPT", "GÖRSEL PROMPT", "GÖRSEL", "IMAGE"])
        extracted_vid = _extract_field(text, ["VIDEO_PROMPT", "VİDEO_PROMPT", "VIDEO PROMPT", "VİDEO PROMPT", "VIDEO", "VİDEO"])
        # Yalnızca geçerli, temiz bir görsel promptu yakalanmışsa tek sahne olarak kabul et
        if (
            extracted_img
            and len(extracted_img) > 20
            and not any(bad in extracted_img.lower() for bad in ["ugc içeriği", "içeriği üret", "dil modeli", "programlanmadım", "yardımcı olamıyorum"])
        ):
            single_title = _extract_field(text, ["BAŞLIK", "TITLE"]) or "Sahne 1"
            vo = _extract_field(text, ["SESLENDIRME", "SESLENDİRME", "VOICEOVER", "VO", "SES"]) or ""
            return [
                SceneItem(
                    index=1,
                    title=single_title,
                    description=_extract_field(text, ["AÇIKLAMA", "DESCRIPTION"]) or text[:200].strip(),
                    duration_seconds=default_seconds,
                    voiceover_text=vo.strip(),
                    image_prompt=extracted_img.strip(),
                    video_prompt=extracted_vid.strip() or (
                        "3D cartoon animation Pixar style, smooth expressive character movement" if is_animation else "Smooth cinematic camera movement"
                    ),
                    status="pending",
                )
            ]
        return _fallback_scenes(default_count, default_seconds, fallback_prompt or text[:300], is_animation=is_animation)

    seen_indices = set()
    for i, match in enumerate(matches):
        idx = int(match.group(1) or match.group(2) or (len(scenes) + 1))
        if idx in seen_indices:
            continue
        seen_indices.add(idx)
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[start:end]

        title = _extract_field(block, ["BAŞLIK", "TITLE"]) or f"Sahne {idx}"
        desc = _extract_field(block, ["AÇIKLAMA", "DESCRIPTION"]) or block[:200].strip()
        img_prompt = _extract_field(block, ["GÖRSEL_PROMPT", "IMAGE_PROMPT", "GÖRSEL PROMPT", "GÖRSEL", "IMAGE"]) or fallback_prompt
        vid_prompt = _extract_field(block, ["VIDEO_PROMPT", "VİDEO_PROMPT", "VIDEO PROMPT", "VİDEO PROMPT", "VIDEO", "VİDEO"]) or (
            "3D cartoon animation Pixar style, smooth expressive movement" if is_animation else "Smooth cinematic movement"
        )
        vo = _extract_field(block, ["SESLENDIRME", "SESLENDİRME", "VOICEOVER", "VO", "SES"]) or ""
        vo_clean = clean_voiceover_for_prompt(vo)
        scenes.append(
            SceneItem(
                index=idx,
                title=title.strip(),
                description=desc.strip(),
                duration_seconds=default_seconds,
                voiceover_text=vo_clean or vo.strip(),
                image_prompt=img_prompt.strip(),
                video_prompt=vid_prompt.strip(),
                status="pending",
            )
        )

    return scenes or _fallback_scenes(default_count, default_seconds, fallback_prompt, is_animation=is_animation)


def _extract_field(text: str, prefixes: list[str]) -> str:
    """Sahne bloğundan istenen alanın değerini çıkarır.
    Değer, bir sonraki alan etiketine (yapışık olsa bile), sahne başlığına, '---' ayracına veya metin sonuna kadar sürer.
    """
    end_boundary = (
        rf"(?=\**(?:{_LABEL_ALT})\**[ \t]*\**:"  # büyük harfli bir sonraki etiket (yapışık olabilir)
        rf"|\n[ \t]*\**(?i:{_LABEL_ALT})\**[ \t]*\**:"  # satır başında herhangi büyüklükte etiket
        r"|\[\s*(?:SAHNE|SCENE)\s*\d+"
        r"|\n[ \t]*---"
        r"|\Z)"
    )
    for prefix in prefixes:
        # Önce birebir (büyük harf) etiket, sonra büyük/küçük harf duyarsız eşleşme dene
        for flags in (0, re.IGNORECASE):
            pat = rf"\**{re.escape(prefix)}\**[ \t]*\**[:\-]\**[ \t]*(.*?){end_boundary}"
            m = re.search(pat, text, re.DOTALL | flags)
            if m:
                val = m.group(1).strip()
                val = val.strip("* \t\r\n")
                return val.split("\n---")[0].strip()
    return ""


def _fallback_scenes(count: int, seconds: int, fallback_prompt: str, is_animation: bool = False) -> list[SceneItem]:
    clean = sanitize_topic_for_prompts(fallback_prompt.strip())
    if any(bad in clean.lower() for bad in ["videonuz hazır", "0:00", "kullanım sınırı", "limit", "hata", "hazır!", "dil modeli", "programlanmadım", "yardımcı olamıyorum"]):
        clean = ""

    # Brief içinden konu veya anahtar kelimeleri çıkar
    topic = clean
    for prefix in ["Masal Konusu:", "Konu:", "Brief:", "Açıklama:"]:
        if prefix.lower() in topic.lower():
            for line in topic.split("\n"):
                if prefix.lower() in line.lower():
                    topic = line.split(":", 1)[-1].strip()
                    break
    topic = sanitize_topic_for_prompts(topic)

    scenes: list[SceneItem] = []
    total = max(1, count)

    if is_animation:
        # Çocuk masalı / animasyon için sahne akışı şablonları
        story_templates = [
            (
                "Giriş ve Meraklı Karşılaşma",
                f"Sevimli ikizler Atakan ve Kumsal neşeli bir ortamda merakla etrafa bakıyor, yeni bir maceraya başlıyor ({topic or 'renkli masal dünyası'}).",
                f"3D cartoon animation Pixar style: Adorable cheerful toddler twins Atakan and Kumsal with big sparkling eyes in a charming vibrant nursery room, soft warm studio lighting, Disney Pixar 3D CGI aesthetic, {topic or 'friendly atmosphere'}, 9:16 vertical orientation",
                "3D cartoon animation Pixar style: The cute characters look around with joyful curiosity, pointing and smiling happily at the camera, smooth expressive movements, 9:16 vertical video",
                "Merhaba arkadaşlar! Bugün Atakan ve Kumsal ile birlikte çok eğlenceli ve öğretici bir maceraya hazır mısınız?",
            ),
            (
                "Keşif ve Eğlenceli Oyun",
                f"Karakterler heyecanla ipuçlarını inceliyor, birbirleriyle neşeyle konuşup gülümseyerek çözüme odaklanıyor.",
                f"3D cartoon animation Pixar style: Adorable animated characters playfully interacting, smiling brightly in a colorful playful setting, soft volumetric studio lighting, 9:16 vertical orientation",
                "3D cartoon animation Pixar style: The characters gesture excitedly, smiling and nodding with lively expressive motions, cheerful friendly animation, 9:16 vertical video",
                "Bakın burada rengarenk ipuçları var! Acaba bu bilmecenin cevabını kimler tahmin edebilecek?",
            ),
            (
                "Neşeli Başarı ve Kutlama",
                f"İkizler sevinçle bilmecenin cevabını bulup el çırparak alkışlıyor ve birbirlerine sarılarak kutluyor.",
                f"3D cartoon animation Pixar style: Cheerful animated characters clapping hands in excitement, huge happy smiles, celebrating together with colorful festive sparkles, warm volumetric lighting, 9:16 vertical orientation",
                "3D cartoon animation Pixar style: The characters jump in delight, clapping hands excitedly, laughing and celebrating with big friendly smiles, lively dynamic motion, 9:16 vertical video",
                "Harikasınız çocuklar! Birlikte düşününce cevabı hemen bulduk, kocaman bir alkış hepimize!",
            ),
            (
                "Dostluk ve Veda Anı",
                f"Karakterler kameraya el sallayarak sıcacık bir gülümsemeyle izleyicilere veda ediyor.",
                f"3D cartoon animation Pixar style: Cute friendly characters standing close together, waving warmly at the camera with joyful expressions, magical glowing ambient light, 9:16 vertical orientation",
                "3D cartoon animation Pixar style: The characters wave gently at the camera, smiling warmly with joyful sparkling eyes, smooth cinematic camera slow pull back, 9:16 vertical video",
                "Bugünkü oyunumuzun sonuna geldik. Yeni masallarda ve oyunlarda tekrar görüşmek üzere, hoşça kalın!",
            ),
        ]
        for i in range(total):
            idx = i % len(story_templates)
            title, desc, img_p, vid_p, vo_p = story_templates[idx]
            suffix = f" (Bölüm {i + 1})" if i >= len(story_templates) else ""
            scenes.append(
                SceneItem(
                    index=i + 1,
                    title=f"Sahne {i + 1}: {title}{suffix}",
                    description=desc,
                    duration_seconds=seconds,
                    voiceover_text=vo_p,
                    image_prompt=img_p,
                    video_prompt=vid_p,
                    status="pending",
                )
            )
    else:
        # Ticari / Ürün / Reklam için sahne akışı şablonları
        comm_templates = [
            (
                "Ürün Tanıtımı ve Estetik Sunum",
                f"Ürünün ({topic or 'özel ürün koleksiyonu'}) modern ve şık stüdyo ortamında estetik sunumu.",
                f"Commercial product photography: Beautiful aesthetic presentation of {topic or 'the hero product'} on a clean minimalist display, soft natural daylight and warm studio rim light, elegant pastel tones, 8k resolution, 9:16 vertical orientation",
                "Cinematic smooth 360-degree orbit around the hero product display, soft reflections, elegant slow motion, 9:16 vertical video",
                "Hayatınıza değer katacak modern tasarım ve üstün kaliteyle hemen tanışın.",
            ),
            (
                "Detay ve Materyal Kalitesi",
                f"Ürünün ({topic or 'ürün'}) dokusal detayları, kaliteli materyali ve özenli işçiliği.",
                f"Macro commercial detail shot: Premium textures, fabric and finish of {topic or 'the product'}, soft golden ambient bokeh, high-end editorial product presentation, 8k resolution, 9:16 vertical orientation",
                "Cinematic slow camera push-in focusing on product details and fine textures, smooth dynamic motion, 9:16 vertical video",
                "Özenle işlenmiş detayları ve dayanıklı yapısıyla her an konforu hissedin.",
            ),
            (
                "Öne Çıkan Özellikler ve Dinamizm",
                f"Ürünün ({topic or 'ürün'}) şık duruşunu ve zarafetini vurgulayan dinamik sahne.",
                f"Dynamic lifestyle commercial shot: {topic or 'Hero product'} showcased in an inspiring modern boutique setting, bright welcoming natural light, 9:16 vertical orientation",
                "Cinematic graceful dolly motion showcasing the complete product in its environment, smooth elegant lighting shift, 9:16 vertical video",
                "Günün her anında zarafet ve işlevselliği bir arada sunan özel bir deneyim.",
            ),
            (
                "Kapanış ve Marka Vurgusu",
                f"Ürünün akılda kalıcı son karesi ve prestijli sunumu.",
                f"Prestigious commercial end-frame: {topic or 'Hero product'} centered with premium glowing rim light, clean minimal background, cinematic depth of field, 9:16 vertical orientation",
                "Cinematic slow camera glide with subtle focus shift on the hero product, soft gradient lighting, 9:16 vertical video",
                "Şimdi keşfedin ve tarzınızı ayrıcalıklı bir seviyeye taşıyın.",
            ),
        ]
        for i in range(total):
            idx = i % len(comm_templates)
            title, desc, img_p, vid_p, vo_p = comm_templates[idx]
            suffix = f" (Bölüm {i + 1})" if i >= len(comm_templates) else ""
            scenes.append(
                SceneItem(
                    index=i + 1,
                    title=f"Sahne {i + 1}: {title}{suffix}",
                    description=desc,
                    duration_seconds=seconds,
                    voiceover_text=vo_p,
                    image_prompt=img_p,
                    video_prompt=vid_p,
                    status="pending",
                )
            )

    return scenes


def build_scene_image_prompt(scene: SceneItem, job: JobOrder) -> str:
    """Aşama 2: Belirli bir sahne için Gemini görsel üretim promptu oluşturur."""
    aspect = "9:16 vertical orientation" if job.video_format == "9:16" else f"{job.video_format} aspect ratio"
    is_anim = is_animation_content_type(job)
    raw_prompt = (scene.image_prompt or "").strip()

    # Meta komutları temizle
    clean_p = sanitize_topic_for_prompts(raw_prompt)

    # Eğer prompt tamamen Türkçe veya ham brief kalıntısı ise doğrudan görsel betimlemeye çevir
    is_mostly_turkish = any(ch in clean_p.lower() for ch in ["ç", "ğ", "ı", "ö", "ş", "ü"]) or any(
        w in clean_p.lower() for w in ["çocuk", "giyim", "çorap", "ürün", "reklam", "sahne", "tanıtım", "için"]
    )

    if is_anim:
        if is_mostly_turkish or len(clean_p) < 25:
            base_prompt = f"3D cartoon animation Pixar style: Charming stylized 3D character and scene depicting {clean_p or 'delightful story adventure'}, vibrant rich colors, soft warm studio lighting, Disney Pixar 3D CGI aesthetic"
        else:
            base_prompt = clean_p if "pixar" in clean_p.lower() else f"3D cartoon animation Pixar style: {clean_p}"
    else:
        if is_mostly_turkish or len(clean_p) < 25:
            base_prompt = f"Commercial product photography: Beautiful aesthetic presentation of {clean_p or 'the product'}, elegant minimalist studio setting, soft daylight and warm ambient rim lighting, 8k resolution"
        else:
            base_prompt = clean_p

    # Aspect ratio ekle (eğer içinde yoksa)
    if "9:16" not in base_prompt and "orientation" not in base_prompt and "aspect" not in base_prompt:
        base_prompt = f"{base_prompt}, {aspect}"

    if job.reference_images:
        if is_anim:
            ref_note = "CRITICAL FIDELITY & STYLE REQUIREMENT: Strictly maintain the character and product design, colors, and features from the attached reference image in this 3D Pixar animation style."
        else:
            ref_note = "CRITICAL FIDELITY REQUIREMENT: Strictly replicate and maintain the exact appearance, branding, colors, and materials of the product in the attached reference image."
        return f"Create an image: {base_prompt}. {ref_note}"

    return f"Create an image: {base_prompt}."


def build_scene_video_prompt(scene: SceneItem, job: JobOrder, has_image: bool = False) -> str:
    """Aşama 3: Belirli bir sahne için Gemini video üretim promptu oluşturur."""
    aspect = "9:16 vertical video" if job.video_format == "9:16" else f"{job.video_format} video"
    is_anim = is_animation_content_type(job)
    standalone_rule = (
        f"STANDALONE SCENE INSTRUCTION (Scene {scene.index} of {job.scene_count}): "
        f"Produce this scene as a completely independent, self-contained cinematic video clip with its own dynamic camera framing, opening, and motion. "
        f"Do NOT treat this as a leftover continuation, extension, or reaction shot of any previous scene. "
        f"All scenes will be professionally merged together in post-production, so this clip must stand on its own as a full {scene.duration_seconds}-second shot."
    )
    style_motion = (
        "3D cartoon animation, Pixar style animation, smooth expressive character movements, lively physics, delightful vibrant lighting"
        if is_anim
        else "smooth camera motion, realistic physics and lighting"
    )

    if has_image:
        return (
            f"Animate the attached reference image into a {aspect} ({scene.duration_seconds} seconds): {scene.video_prompt}. "
            f"Use the attached image as the starting frame and scene reference. "
            f"Keep {style_motion}, and remain completely faithful to the character and product design. "
            f"{standalone_rule}"
        )
    ref_desc = "faithful to the product design and visual prompt" if not job.reference_images else "faithful to the product/character in the reference image"
    return (
        f"Create a {aspect} ({scene.duration_seconds} seconds): {scene.video_prompt}. "
        f"{style_motion}, {ref_desc}. "
        f"{standalone_rule}"
    )


POLICY_SANITIZATION_MAP = [
    # Medikal / Klinik / Cerrahi
    (r"\b(?:inject(?:ion|ing|ed|s)?|needle|syringe|filler|lip filler|botox|dermal filler)\b", "modern aesthetic clinic ambiance, soft radiance and subtle healthy skin glow"),
    (r"\b(?:surgery|surgical|operation|incision|scalpel|wound|blood|bleeding)\b", "pristine luxury architectural clinic interior, serene minimalist wellness environment"),
    (r"\b(?:treatment on face|procedure on skin|laser on face|doctor touching face|doctor examining patient)\b", "radiant healthy skin aesthetics, elegant cinematic slow dolly in a modern wellness lounge"),
    (r"\b(?:patient|illness|disease|pain|suffering)\b", "satisfied client, serene atmosphere, calm luxury setting"),
    
    # Şiddet / Çatışma / Saldırı
    (r"\b(?:fight(?:ing)?|punch(?:ing)?|hit(?:ting)?|attack(?:ing)?|strike)\b", "energetic dynamic camera movement, sweeping cinematic motion"),
    (r"\b(?:gun|weapon|knife|sword|shoot(?:ing)?|kill(?:ing)?|murder)\b", "dramatic cinematic lighting shift, intense lens flare, heroic framing"),
    (r"\b(?:destroy(?:ing)?|explosion|explod(?:e|ing)|burn(?:ing)?|fire)\b", "dramatic lighting burst, warm golden hour atmospheric illumination"),
    
    # Müstehcen / Hassas fiziksel temas
    (r"\b(?:naked|nude|sexy|sensual|intimate|bikini|bed scene|kissing)\b", "sophisticated fashion aesthetic, elegant graceful movement, premium ambient lighting"),
    
    # Tehlikeli hareketler
    (r"\b(?:falling from height|crash(?:ing)?|drown(?:ing)?)\b", "smooth graceful camera swoop, lively cinematic motion"),
]


def sanitize_prompt_text_for_policy(text: str) -> str:
    """Metindeki video politikasına takılabilecek hassas/tıbbi/şiddet içeren kelimeleri güvenli sinematik alternatiflerle değiştirir."""
    sanitized = text
    for pattern, replacement in POLICY_SANITIZATION_MAP:
        sanitized = re.sub(pattern, replacement, sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\s+", " ", sanitized).strip()
    return sanitized


def revise_scene_video_prompt_for_safety(
    scene: SceneItem,
    job: JobOrder,
    refusal_reason: str = "",
    attempt: int = 1,
    has_image: bool = False,
) -> tuple[str, str]:
    """Gemini video politika uyarısı (policy refusal) alındığında senaryoyu ve video promptunu
    bütünlüğü koruyarak Gemini kurallarına uygun şekilde yeniden düzenler.
    
    Dönüş: (yeni_video_promptu, yeni_sahne_aciklamasi)
    """
    is_anim = is_animation_content_type(job)
    content_type = (job.content_type or "").lower()

    # 1. Mevcut prompt ve açıklamadaki riskli kelimeleri temizle
    safe_base = sanitize_prompt_text_for_policy(scene.video_prompt or scene.description or scene.title)
    
    # 2. Deneme seviyesine ve içerik türüne göre politika uyumlu sinematik aksiyon oluştur
    if is_anim:
        # Animasyon / Masal: Sevimli, neşeli, tehlikesiz Pixar 3D karakter hareketi
        if attempt == 1:
            safe_action = f"3D cartoon animation Pixar style, charming stylized character cheerful gentle motion, lively delightful atmosphere, soft volumetric lighting, smooth camera motion: {safe_base}"
        else:
            safe_action = "3D cartoon animation Pixar style, charming friendly character looking happily at the camera with a gentle warm smile and cheerful wave, lively playful physics, soft volumetric studio illumination, smooth camera slow push-in"
    elif "klinik" in content_type:
        # Klinik videosu: Tıbbi müdahale / iğne içermeyen, prestijli, mimari ve estetik atmosfer
        if attempt == 1:
            safe_action = f"Cinematic slow camera glide through a high-end luxury aesthetic clinic lounge, soft ambient lighting reflecting radiant healthy skin beauty, serene peaceful wellness atmosphere: {safe_base}"
        else:
            safe_action = "Cinematic slow horizontal pan across a luxury modern clinic interior, soft daylight through floor-to-ceiling windows, pristine marble and minimalist architecture, serene and calming atmosphere"
    elif "ürün" in content_type or "reklam" in content_type:
        # Ürün reklamı: 360 dönüş, stüdyo aydınlatması, zarif kamera hareketi
        if attempt == 1:
            safe_action = f"Cinematic smooth camera orbit around the premium product display, clean studio lighting with soft reflections, subtle depth of field bokeh, elegant commercial presentation: {safe_base}"
        else:
            safe_action = "Cinematic 360-degree slow rotation of the hero product on a minimalist studio pedestal, soft gradient lighting, elegant bokeh, clean commercial aesthetic"
    else:
        # Genel / Reels / Diğer
        if attempt == 1:
            safe_action = f"Cinematic smooth camera motion, elegant slow dolly forward, professional studio lighting, peaceful atmospheric bokeh, clean aesthetic: {safe_base}"
        else:
            safe_action = "Cinematic slow pan across the aesthetic scene, soft golden ambient lighting, elegant camera drift, premium high production value"

    # Güvenlik ve video politikası uyumluluk eki
    safe_action = sanitize_prompt_text_for_policy(safe_action)
    safe_action += ", clean cinematic aesthetic, fully compliant with video policy guidelines, smooth realistic motion"

    # Sahne açıklamasını da Türkçe olarak güncelle (bütünlük korunarak)
    revised_desc = f"[Politika Uyarlaması {attempt}] {scene.title} - Güvenli sinematik kamera hareketi ve estetik atmosfer sahnesi"

    # Sahne nesnesinin video_prompt ve description alanlarını güncelle
    scene.video_prompt = safe_action
    scene.description = revised_desc

    # Gemini'ye gönderilecek nihai promptu build_scene_video_prompt ile derle
    use_image = has_image if attempt == 1 else False
    final_prompt = build_scene_video_prompt(scene, job, has_image=use_image)

    return final_prompt, revised_desc




def build_production_prompt(job: JobOrder) -> str:
    """Geriye dönük uyumluluk için tekil prompt oluşturucu."""
    ref_names = [Path(p).name for p in job.reference_images]
    voice = "Evet, Türkçe seslendirme iste." if job.voiceover else "Seslendirme isteme."
    music = "Evet, uygun müzik iste." if job.music else "Müzik ekleme."
    scenario_block = job.scenario.strip() or "(hazır senaryo verilmedi; brief'ten sahne planı öner)"
    brief_block = job.brief.strip() or "(brief boş)"
    notes = job.editing_notes.strip() or "(kurgu notu yok)"
    extra = job.extra_notes.strip() or "(ek not yok)"
    refs_block = ", ".join(ref_names) if ref_names else "(referans görsel yok)"
    anim_rule = ""
    if is_animation_content_type(job):
        anim_rule = "\n6. Animasyon Stili: 3D cartoon animasyon, Pixar tarzı 3D modelleme ve animasyon (brief veya açıklamada yazmasa dahi animasyon içeriği Pixar tarzı 3D animasyon olarak üretilmelidir)."

    return f"""DuyguAI Studio müşteri içerik üretimi iş emri.

İçerik türü: {job.content_type}
Video formatı: {job.video_format}
Sahne sayısı: {job.scene_count}
Sahne başına süre: {job.seconds_per_scene} saniye
Seslendirme: {voice}
Müzik: {music}
Kurgu tercihleri: {notes}

Müşteri brief/açıklama:
{brief_block}

Hazır senaryo:
{scenario_block}

Ek notlar:
{extra}

Referans görseller (dosya adları): {refs_block}

İstek:
1. Brief ve varsa hazır senaryoyu koru; sabit bir ürün-reklam kalıbına çevirme.
2. {job.scene_count} sahnelik bir sahne planı yaz (her sahne için görsel talimat + kamera + süre).
3. Referans görseller varsa ürüne/kişiye sadık kal. Yoksa brief'teki betimlemeyi kullan.
4. {job.video_format} formatında video üret.
5. Seslendirme/müzik/kurgu yalnızca istendiyse uygula.{anim_rule}

Bu mesaj bir iş emridir. Cevabında sahne planını açık metin olarak da yaz.
"""

