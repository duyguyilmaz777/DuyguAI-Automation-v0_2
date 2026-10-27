from __future__ import annotations

import base64
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from providers.base import (
    BrowserCrashedError,
    GemAccessDeniedError,
    HighTrafficCapacityError,
    ProviderError,
    RateLimitError,
    SafetyRefusalError,
    SessionExpiredError,
    TransientGeminiError,
    VideoPolicyRefusalError,
)
from providers.video_sources import is_new_http_video
from services.files import extract_drive_folder_id


HIGH_TRAFFIC_TRIGGERS = [
    "unusually high traffic",
    "high traffic right now",
    "generating videos and am at full capacity",
    "at full capacity",
    "full capacity",
    "experiencing unusually high traffic",
    "high traffic",
    "capacity right now",
    "full capacity. please try again later",
    "yüksek trafik",
    "tam kapasitede",
    "tam kapasitedeyiz",
    "tam kapasitedeyim",
    "yoğun trafik",
    "aşırı yoğunluk",
    "sunucu yoğunluğu",
]


VIDEO_POLICY_REFUSAL_TRIGGERS = [
    "i can't generate that video",
    "can't generate that video",
    "try describing another idea",
    "video policy guidelines",
    "review our video policy",
    "review our video policy guidelines",
    "unable to generate that video",
    "cannot generate that video",
    "i cannot generate that video",
    "can't generate video",
    "cannot generate video",
    "bu videoyu oluşturamıyorum",
    "başka bir fikir açıklamayı deneyin",
    "video politikası kurallarımızı",
    "video politikası kuralları",
    "video politikası",
    "video policy",
    "policy guidelines",
    "politikası kuralları",
]

REFUSAL_TRIGGERS = [
    "dil modeli olarak",
    "yardımcı olmak üzere tasarlanmadım",
    "i am a language model",
    "i cannot fulfill",
    "against our policies",
    "unable to generate",
    "bu konuda yardımcı olamıyorum",
    "yardımcı olamam",
    "yardımcı olamıyorum",
    "programlanmadım",
    "isteğinizi gerçekleştirebilecek şekilde",
    "yalnızca metin oluşturuyorum",
    "yalnızca metin üretiyorum",
    "sadece metin oluşturuyorum",
    "sadece metin üretiyorum",
    "yalnızca bir dil modeli",
    "bir dil modeli olduğum",
    "bir dil modeli olduğum için",
    "bir dil modeli olduğumdan",
    "bu isteğinize yardımcı olamıyorum",
    "gerekli bilgiye ya da beceriye sahip olmadığım",
    "not programmed to",
    "only generate text",
    "i only generate text",
    "i am only a language model",
    "i'm only a language model",
]


RATE_LIMIT_TRIGGERS = [
    "limit reached",
    "usage limit",
    "kullanım sınırına ulaştınız",
    "daha sonra tekrar deneyin",
    "quota exceeded",
    "too many requests",
    "try again in",
]


TRANSIENT_ERROR_TRIGGERS = [
    # Gemini İngilizce hata ve sorun ifadeleri
    "i seem to be encountering an error",
    "seem to be encountering an error",
    "encountering an error",
    "encountered an error",
    "having a hard time",
    "hard time fulfilling",
    "can i help you with something else",
    "help you with something else instead",
    "something else instead",
    "can i try something else for you",
    "try something else for you",
    "can i try something else",
    "try something else",
    "something went wrong",
    "an error occurred",
    "an error has occurred",
    "internal error",
    "failed to generate",
    "unable to process",
    "please try again",
    "could you try again",
    "try your request again",
    "i ran into an error",
    "ran into an error",
    "ran into a problem",
    "having trouble generating",
    "trouble generating",
    "having trouble",
    # Gemini Türkçe hata ve sorun ifadeleri
    "bir şeyler ters gitti",
    "bir hata oluştu",
    "hata oluştu",
    "sorun oluştu",
    "bir sorunla karşılaştım",
    "hata ile karşılaştım",
    "hata ile karşılaşıldı",
    "bir hatayla karşılaşıyorum",
    "isteğinizi yerine getirmekte zorlanıyorum",
    "başka bir konuda yardımcı",
    "geçici bir hata",
    "sunucu hatası",
    "lütfen tekrar deneyin",
    "tekrar deneyin",
    "tekrar deneyebilir misiniz",
    "başka bir şey deneyebilir miyim",
    "sizin için başka bir şey deneyebilir miyim",
    "başka bir şey deneyelim mi",
]


def sanitize_gem_url(url: str) -> str:
    """Gem URL'sinden varsa eski sohbet ID'sini temizler (örn: /gem/ID/chat_id -> /gem/ID)."""
    if not url:
        return ""
    m = re.match(r"(https?://gemini\.google\.com/gem/[a-zA-Z0-9_-]+)", url.strip())
    if m:
        return m.group(1)
    return url.strip()


def _retry(fn, retries: int, log: Callable[[str], None], what: str):
    last = None
    attempts = max(0, int(retries)) + 1
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last = exc
            log(f"{what} başarısız ({i + 1}/{attempts}): {exc}")
            if i + 1 < attempts:
                time.sleep(min(2 * (i + 1), 6))
    if last is not None:
        raise last
    raise RuntimeError(f"{what} başarısız oldu (deneme kalmadı)")


class GeminiWebProvider:
    """Gemini web arayüzüne mevcut Chrome CDP oturumu üzerinden bağlanır."""

    _active_instances: set[GeminiWebProvider] = set()

    @classmethod
    def abort_all(cls) -> None:
        for inst in list(cls._active_instances):
            try:
                inst.abort()
            except Exception:
                pass

    def __init__(self, cfg: dict, log: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.log = log or (lambda msg: None)
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._last_prompt_response = ""
        self._baseline_response_count = 0
        self.is_aborted = False

    def abort(self) -> None:
        self.is_aborted = True
        self.log("🛑 Gemini işlemleri durduruldu.")

    def connect(self) -> None:
        GeminiWebProvider._active_instances.add(self)
        from playwright.sync_api import sync_playwright

        timeout = int(self.cfg.get("connect_timeout_ms", 20000))
        retries = int(self.cfg.get("retry_limit", 2))
        debug_url = self.cfg.get("chrome_debug_url", "http://127.0.0.1:9222")

        def _open():
            if self._playwright is None:
                self._playwright = sync_playwright().start()
            self.log("Chrome CDP bağlantısı kuruluyor…")
            self._browser = self._playwright.chromium.connect_over_cdp(
                debug_url, timeout=timeout
            )
            contexts = self._browser.contexts
            if not contexts:
                raise ProviderError("Chrome context bulunamadı. Chrome'un CDP ile açık olduğundan emin olun.")
            self._context = contexts[0]
            pages = self._context.pages
            # Mevcut açık Gemini sayfasını bul (bot sorry sayfası hariç)
            gem_page = next((pg for pg in pages if "gemini.google.com" in pg.url and "sorry" not in pg.url), None)
            raw_url = (self.cfg.get("gem_url") or "").strip()
            gem_url = sanitize_gem_url(raw_url)
            if not gem_url:
                raise ProviderError("Gemini Gem URL'si boş.")

            if gem_page is not None:
                self._page = gem_page
                current_url = gem_page.url
                self.log(f"Mevcut Gemini sayfası bağlandı: {current_url}")
                self.log(f"Temiz bir Gemini Gem üretim oturumu açılıyor: {gem_url}")
                gem_page.goto(gem_url, wait_until="domcontentloaded", timeout=60000)
                gem_page.wait_for_timeout(2500)
            else:
                self._page = self._context.new_page()
                self.log(f"Yeni sekmede Gemini açılıyor: {gem_url}")
                self._page.goto(gem_url, wait_until="domcontentloaded", timeout=60000)
            self._page.wait_for_timeout(2500)

            # Oturum ve Gem erişim denetimi
            current_url = getattr(self._page, "url", "")
            if "accounts.google.com" in current_url or "signin" in current_url:
                raise SessionExpiredError("Google oturumu sona ermiş; Chrome profilinde yeniden giriş yapılması gerekiyor.")

            try:
                page_text = self._page.content().lower()
                if "bu gem mevcut değil" in page_text or "this gem isn't available" in page_text:
                    raise GemAccessDeniedError(f"Gem ({gem_url}) bulunamadı veya bu hesaba erişim izni verilmemiş.")
                if "erişim izni isteyin" in page_text or "request access" in page_text:
                    raise GemAccessDeniedError(f"Gem ({gem_url}) için hesap sahibinden paylaşım izni istenmesi gerekiyor.")
            except (SessionExpiredError, GemAccessDeniedError):
                raise
            except Exception:
                pass

        try:
            _retry(_open, retries, self.log, "Gemini CDP bağlantısı")
        except Exception as exc:
            self.close()
            raise ProviderError(
                "Gemini/Chrome bağlantısı kurulamadı. CAPTCHA, giriş veya güvenlik onayları otomatik aşılmaz. "
                f"Ayrıntı: {exc}"
            ) from exc

    def reset_chat_session(self) -> None:
        """Yeni bir iş emri için Gemini sohbet oturumunu sıfırlar."""
        page = self._require_page()
        gem_url = sanitize_gem_url(self.cfg.get("gem_url") or "")
        if gem_url:
            self.log(f"Temiz Gemini oturumu başlatılıyor: {gem_url}")
            page.goto(gem_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
        self._last_prompt_response = ""
        self._baseline_response_count = 0

    def snapshot_videos(self) -> set[str]:
        return self._get_video_sources()

    def snapshot_images(self) -> set[str]:
        return self._get_image_sources()

    def get_image_count(self) -> int:
        page = self._require_page()
        try:
            return page.locator("img.image.loaded, img.image").count()
        except Exception:
            return 0

    def wait_until_idle(self, timeout_seconds: int = 30) -> None:
        """Modelin yanıt vermeyi bitirmesini bekler (Stop/Durdur butonunun kaybolması)."""
        page = self._require_page()
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if getattr(self, "is_aborted", False):
                raise InterruptedError("İşlem kullanıcı tarafından durduruldu.")
            stop_btn = page.locator("button[aria-label*='durdur' i], button[aria-label*='stop' i], .stop-button, [data-test-id='stop-generating-button']")
            thinking = page.locator("thinking-dots-animation, thinking-overlay, [data-test-id*='thinking']")
            is_busy = False
            try:
                if (stop_btn.count() > 0 and stop_btn.first.is_visible()) or (thinking.count() > 0 and thinking.first.is_visible()):
                    is_busy = True
            except Exception:
                pass
            if not is_busy:
                break
            page.wait_for_timeout(1000)

    @staticmethod
    def _looks_like_scene_plan(text: str) -> bool:
        """Metin Gemini'nin ürettiği gerçek bir sahne planıysa True döner.
        Bu durumda senaryo içindeki sıradan kelimeler (örn. 'tekrar deneyin', 'video', 'dil modeli') ret/hata sayılmaz.
        """
        if not text or len(text) < 40:
            return False
        has_marker = re.search(r"(?i)(?:\[\s*(?:SAHNE|SCENE)\s*\d+|(?:^|\n)\s*(?:SAHNE|SCENE)\s*\d+\b)", text) is not None
        has_field = re.search(
            r"(?i)(?:BAŞLIK|AÇIKLAMA|GÖRSEL_PROMPT|IMAGE_PROMPT|VIDEO_PROMPT|VİDEO_PROMPT|SESLENDIRME|SESLENDİRME)\s*\**\s*:",
            text,
        ) is not None
        return has_marker and has_field

    def check_text_safety_or_limits(self, text: str | None) -> None:
        if not text:
            return
        if self._looks_like_scene_plan(text):
            return
        lower = text.lower()
        for trig in HIGH_TRAFFIC_TRIGGERS:
            if trig in lower:
                raise HighTrafficCapacityError(f"Gemini video sunucusu yoğunluğu / tam kapasite: '{text.strip()[:180]}…'")
        for trig in VIDEO_POLICY_REFUSAL_TRIGGERS:
            if trig in lower:
                raise VideoPolicyRefusalError(f"Gemini video politikası uyarısı verdi: '{text.strip()[:180]}…'")
        for trig in REFUSAL_TRIGGERS:
            if trig in lower:
                raise SafetyRefusalError(f"Gemini promptu reddetti: '{text.strip()[:180]}…'")
        for trig in RATE_LIMIT_TRIGGERS:
            if trig in lower:
                raise RateLimitError(f"Gemini kullanım sınırına takıldı: '{text.strip()[:180]}…'")
        for trig in TRANSIENT_ERROR_TRIGGERS:
            if trig in lower and not any(rt in lower for rt in RATE_LIMIT_TRIGGERS):
                raise TransientGeminiError(f"Gemini geçici hata uyarısı: '{text.strip()[:180]}…'")


    def attach_references(self, refs: list[str]) -> None:
        page = self._require_page()
        if not refs:
            self.log("Eklenecek referans görseli yok; bu adım atlandı.")
            return

        inputs = page.locator('input[type="file"][accept*="image"]')
        if not inputs.count():
            inputs = page.locator('input[type="file"]')

        if not inputs.count():
            for name in ["Yükleme ve araçlar", "Upload files", "Add files", "Ekle"]:
                try:
                    btn = page.get_by_role("button", name=re.compile(re.escape(name), re.I)).first
                    if btn.is_visible():
                        btn.click(timeout=1800)
                        page.wait_for_timeout(500)
                        break
                except Exception:
                    continue
            inputs = page.locator('input[type="file"][accept*="image"]')
            if not inputs.count():
                inputs = page.locator('input[type="file"]')

        if inputs.count():
            try:
                inputs.first.set_input_files([str(p) for p in refs])
                # Yüklemenin tamamlanmasını bekle (thumbnail veya kaldır butonu belirene kadar)
                attachment_loc = page.locator(
                    '[aria-label*="Remove attachment"], [aria-label*="Kaldır"], [data-test-id*="attachment"], .attachment-card'
                )
                for _ in range(15):
                    if attachment_loc.count() >= len(refs):
                        break
                    page.wait_for_timeout(500)
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                self.log(f"{len(refs)} görsel Gemini'ye eklendi ve doğrulandı.")
                return
            except Exception as e:
                self.log("Dosya ekleme alanı kullanılamadı: " + str(e))

        attachments = page.locator(
            '[aria-label*="Remove attachment"], [aria-label*="Kaldır"], [data-test-id*="attachment"]'
        )
        try:
            attached = attachments.count()
        except Exception:
            attached = 0
        if attached >= len(refs):
            self.log(f"Ekler görünür: {attached}.")
            return
        raise ProviderError(
            "Referans görselleri Gemini'ye eklenemedi. Seçiciler değişmiş olabilir; üretim isteği gönderilmedi."
        )

    def send_prompt(self, prompt: str) -> None:
        page = self._require_page()
        if not (prompt or "").strip():
            raise ProviderError("Prompt boş; üretim isteği gönderilmedi.")

        # Önceden ekranda kalan son yanıt metnini kaydet (yeni yanıtı teyit etmek için)
        self._last_prompt_response = self.read_response_text(latest_only=True) or ""
        self._baseline_response_count = self._count_model_responses()

        # 1. Modelin önceki üretimi tamamlamasını bekle
        self.wait_until_idle(timeout_seconds=30)

        candidates = [
            page.locator('div[contenteditable="true"]'),
            page.locator("textarea"),
            page.locator('[role="textbox"]'),
        ]
        editor = None
        for loc in candidates:
            try:
                for i in range(loc.count()):
                    item = loc.nth(i)
                    if item.is_visible() and item.is_enabled():
                        editor = item
                        break
                if editor:
                    break
            except Exception:
                pass
        if editor is None:
            raise ProviderError("Gemini prompt alanı bulunamadı.")

        editor.click()
        page.wait_for_timeout(200)
        try:
            editor.fill(prompt, timeout=5000)
        except Exception:
            editor.press("ControlOrMeta+A")
            editor.type(prompt, delay=1)

        # Angular input event tetikleme
        try:
            editor.evaluate("el => el.dispatchEvent(new Event('input', { bubbles: true }))")
        except Exception:
            pass

        # Metnin gerçekten alana yazıldığını teyit et; boşsa tekrar yaz
        try:
            typed = (editor.inner_text(timeout=1000) or "").strip()
            if not typed:
                editor.click()
                editor.type(prompt, delay=1)
        except Exception:
            pass

        page.wait_for_timeout(400)

        sent = False
        deadline = time.time() + 10
        while time.time() < deadline and not sent:
            for name in ["Mesaj gönder", "Send message", "Send", "Gönder"]:
                try:
                    btn = page.get_by_role("button", name=re.compile(re.escape(name), re.I)).last
                    if btn.is_visible() and btn.is_enabled():
                        btn.click(timeout=2500)
                        sent = True
                        break
                except Exception:
                    pass
            if not sent:
                page.wait_for_timeout(1000)

        if not sent:
            try:
                editor.press("ControlOrMeta+Enter")
                sent = True
            except Exception:
                pass

        if not sent:
            raise ProviderError("Prompt dolduruldu ancak gönderme düğmesi etkinleşmedi.")

        page.wait_for_timeout(2000)

    def wait_for_new_image(
        self, timeout_minutes: int = 1, old_sources: set[str] | None = None, initial_count: int = 0
    ) -> str | None:
        """Gemini tarafından yeni üretilen görseli (img.image.loaded) bekler."""
        page = self._require_page()
        old_sources = set(old_sources or set())
        # Halihazırda sayfada görünen tüm görsel kaynaklarını old_sources'a ekle
        try:
            for s in self._get_image_sources():
                old_sources.add(s)
        except Exception:
            pass

        start_time = time.time()
        deadline = start_time + max(1, int(timeout_minutes)) * 60
        check_count = 0
        # Modelin yeni görseli üretmeye başlaması için kısa bekleme (1 sn)
        page.wait_for_timeout(1000)

        while time.time() < deadline:
            if getattr(self, "is_aborted", False):
                raise InterruptedError("İşlem kullanıcı tarafından durduruldu.")
            check_count += 1
            elapsed = time.time() - start_time

            # Hızlı limit dışı geçici hata kontrolü
            transient_err = self.detect_transient_error()
            if transient_err:
                self.log(f"Gemini geçici hata uyarısı tespit edildi (limit değil): {transient_err[:160]}…")
                raise TransientGeminiError(f"Gemini geçici hata uyarısı: {transient_err.strip()[:180]}")

            resp = self.read_response_text(latest_only=True)
            baseline_text = (getattr(self, "_last_prompt_response", "") or "").strip()
            if resp and resp.strip() != baseline_text:
                self.check_text_safety_or_limits(resp)

            # 1. Öncelik: En son model yanıt konteyneri (model-response) içindeki görsel
            try:
                last_turn = page.locator("model-response").last
                if last_turn.count() > 0:
                    turn_imgs = last_turn.locator("img.image.loaded, img.image")
                    if turn_imgs.count() > 0:
                        src = turn_imgs.last.get_attribute("src") or ""
                        if src and src not in old_sources and (elapsed >= 2.0 or initial_count == 0):
                            self.log(f"Gemini son yanıt konteynerinde yeni görsel tespit edildi.")
                            return src
            except Exception:
                pass

            # 2. Genel sayfa kontrolü: SADECE count > initial_count ise ve yeni src ise kabul et!
            imgs = page.locator("img.image.loaded, img.image")
            count = imgs.count()
            if count > initial_count and (elapsed >= 2.0 or initial_count == 0):
                last_img = imgs.last
                src = last_img.get_attribute("src") or ""
                if src and src not in old_sources:
                    self.log(f"Gemini tarafından üretilen yeni görsel tespit edildi ({count}. görsel).")
                    return src
            page.wait_for_timeout(1000)
        return None

    def save_latest_generated_image(self, dest_path: str | Path) -> bool:
        """Gemini tarafından üretilen en son görseli doğrudan tam çözünürlüklü orijinal dosya olarak kaydeder."""
        page = self._require_page()
        target = Path(dest_path)
        target.parent.mkdir(parents=True, exist_ok=True)

        # 1. Öncelik: Gemini'nin "Tam boyutlu resmi indir" butonunu CDP ile tetikleyip orijinal dosyayı indirmek
        try:
            btns = None
            last_turn = page.locator("model-response").last
            if last_turn.count() > 0:
                turn_btns = last_turn.locator(
                    "button[aria-label*='resmi indir' i], "
                    "button[aria-label*='download' i], "
                    "[data-test-id='download-generated-image-button']"
                )
                if turn_btns.count() > 0:
                    btns = turn_btns

            if not btns or btns.count() == 0:
                btns = page.locator(
                    "button[aria-label*='resmi indir' i], "
                    "button[aria-label*='download' i], "
                    "[data-test-id='download-generated-image-button']"
                )

            btn_count = btns.count() if btns else 0
            if btn_count > 0:
                last_btn = btns.last
                try:
                    last_btn.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                page.wait_for_timeout(500)

                tmp_dl_dir = Path("/tmp/gemini_downloads")
                tmp_dl_dir.mkdir(parents=True, exist_ok=True)
                before_files = set(tmp_dl_dir.iterdir())

                # CDP oturumu üzerinden indirme davranışını ayarla
                cdp = page.context.new_cdp_session(page)
                try:
                    cdp.send("Browser.setDownloadBehavior", {
                        "behavior": "allowAndName",
                        "downloadPath": str(tmp_dl_dir),
                        "eventsEnabled": True,
                    })
                except Exception:
                    try:
                        cdp.send("Page.setDownloadBehavior", {
                            "behavior": "allow",
                            "downloadPath": str(tmp_dl_dir),
                        })
                    except Exception:
                        pass

                last_btn.click()

                # İndirilen orijinal dosyanın tamamlanmasını bekle
                downloaded_file = None
                for _ in range(30):
                    time.sleep(0.3)
                    diff = set(tmp_dl_dir.iterdir()) - before_files
                    ready = [f for f in diff if not f.name.endswith(".crdownload") and f.is_file() and f.stat().st_size > 30000]
                    if ready:
                        downloaded_file = ready[0]
                        break

                if downloaded_file and downloaded_file.exists():
                    orig_target = target.with_suffix(".orig.jpg")
                    try:
                        shutil.copy2(downloaded_file, orig_target)
                    except Exception:
                        pass

                    # Hedef format PNG ise sips ile kayıpsız çevir, değilse doğrudan kopyala
                    if target.suffix.lower() == ".png":
                        res = subprocess.run(
                            ["sips", "-s", "format", "png", str(downloaded_file), "--out", str(target)],
                            capture_output=True,
                        )
                        if res.returncode != 0:
                            shutil.copy2(downloaded_file, target)
                    else:
                        shutil.copy2(downloaded_file, target)

                    try:
                        downloaded_file.unlink()
                    except Exception:
                        pass

                    if target.exists() and target.stat().st_size > 10000:
                        self.log(
                            f"Gemini orijinal tam çözünürlüklü görsel indirildi: {target.name} "
                            f"({target.stat().st_size / 1024:.1f} KB)"
                        )
                        return True
        except Exception as e:
            self.log(f"Orijinal görsel indirme butonu denenirken hata (alternatife geçiliyor): {e}")

        # 2. Alternatif: Canvas üzerinden naturalWidth x naturalHeight piksel aktarımı
        try:
            last_img = None
            last_turn = page.locator("model-response").last
            if last_turn.count() > 0:
                turn_imgs = last_turn.locator("img.image.loaded, img.image, img[src*='blob']")
                if turn_imgs.count() > 0:
                    last_img = turn_imgs.last
            if not last_img:
                imgs = page.locator("img.image.loaded, img.image, img[src*='blob']")
                if imgs.count() > 0:
                    last_img = imgs.last

            if last_img:
                try:
                    last_img.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                page.wait_for_timeout(1000)
                res = last_img.evaluate("""(img) => {
                    try {
                        const canvas = document.createElement('canvas');
                        canvas.width = img.naturalWidth || img.width;
                        canvas.height = img.naturalHeight || img.height;
                        const ctx = canvas.getContext('2d');
                        ctx.drawImage(img, 0, 0);
                        return { ok: true, dataUrl: canvas.toDataURL('image/png') };
                    } catch (e) {
                        return { ok: false, error: e.toString() };
                    }
                }""")
                if res and res.get("ok") and "dataUrl" in res:
                    raw = base64.b64decode(res["dataUrl"].split(",", 1)[1])
                    if len(raw) > 10000:
                        target.write_bytes(raw)
                        self.log(f"Gemini görseli piksel düzeyinde canvas üzerinden kaydedildi: {target.name} ({len(raw) / 1024:.1f} KB)")
                        return True
        except Exception as e:
            self.log(f"Canvas aktarımı denenirken hata: {e}")

        # 3. Son çare fallback: Element screenshot
        try:
            last_img = None
            last_turn = page.locator("model-response").last
            if last_turn.count() > 0:
                turn_imgs = last_turn.locator("img.image.loaded, img.image")
                if turn_imgs.count() > 0:
                    last_img = turn_imgs.last
            if not last_img:
                imgs = page.locator("img.image.loaded, img.image")
                if imgs.count() > 0:
                    last_img = imgs.last

            if last_img:
                try:
                    last_img.scroll_into_view_if_needed(timeout=5000)
                except Exception:
                    pass
                page.wait_for_timeout(1000)
                last_img.screenshot(path=str(target))
                if target.exists() and target.stat().st_size > 10000:
                    self.log(f"Gemini görseli ekran görüntüsü olarak kaydedildi (son çare): {target.name}")
                    return True
        except Exception as e:
            self.log(f"Görsel kaydetme hatası: {e}")
        return False

    def detect_video_policy_refusal(self) -> str | None:
        """Sayfada veya son model yanıtında video politika reddi uyarısı olup olmadığını hızlıca tespit eder.
        Eski model yanıtlarını ve kullanıcının kendi gönderdiği prompt metinlerini hariç tutar.
        """
        page = self._require_page()
        baseline_text = (getattr(self, "_last_prompt_response", "") or "").strip()

        # 1. En son model yanıt metnini kontrol et (yalnızca yeni bir model yanıtı geldiyse)
        try:
            resp = (self.read_response_text(latest_only=True) or "").strip()
            if resp and resp != baseline_text:
                lower = resp.lower()
                for trig in VIDEO_POLICY_REFUSAL_TRIGGERS:
                    if trig in lower:
                        return resp
        except Exception:
            pass

        # 2. Model yanıtları veya alert öğeleri içindeki hata/politika metinlerini ara (kullanıcı sorguları hariç)
        refusal_trigs = [
            "can't generate that video",
            "cannot generate that video",
            "try describing another idea",
            "review our video policy",
            "bu videoyu oluşturamıyorum",
            "başka bir fikir açıklamayı deneyin",
            "video politikası kurallarımızı",
            "video politikası kuralları",
        ]
        try:
            containers = page.locator("model-response, [role='alert'], .error-banner, [data-test-id='model-response-text']")
            if containers.count() > 0:
                item = containers.last
                if item.is_visible():
                    txt = (item.inner_text(timeout=500) or "").strip()
                    if txt and txt != baseline_text:
                        lower = txt.lower()
                        for trig in refusal_trigs:
                            if trig in lower:
                                return txt
        except Exception:
            pass

        return None

    def detect_high_traffic_capacity(self) -> str | None:
        """Sayfada veya son model yanıtında geçici yüksek trafik / tam kapasite uyarısı olup olmadığını tespit eder.
        Eski model yanıtlarını ve kullanıcının kendi gönderdiği prompt metinlerini hariç tutar.
        """
        page = self._require_page()
        baseline_text = (getattr(self, "_last_prompt_response", "") or "").strip()

        # 1. En son model yanıt metnini kontrol et
        try:
            resp = (self.read_response_text(latest_only=True) or "").strip()
            if resp and resp != baseline_text:
                lower = resp.lower()
                for trig in HIGH_TRAFFIC_TRIGGERS:
                    if trig in lower:
                        return resp
        except Exception:
            pass

        # 2. Model yanıtları veya alert öğeleri içindeki hata/trafik metinlerini tara
        try:
            containers = page.locator("model-response, [role='alert'], .error-banner, [data-test-id='model-response-text']")
            if containers.count() > 0:
                item = containers.last
                if item.is_visible():
                    txt = (item.inner_text(timeout=500) or "").strip()
                    if txt and txt != baseline_text:
                        lower = txt.lower()
                        for trig in HIGH_TRAFFIC_TRIGGERS:
                            if trig in lower:
                                return txt
        except Exception:
            pass

        return None

    def detect_transient_error(self) -> str | None:
        """Sayfada veya son model yanıtında limit dışı geçici hata veya uyarı olup olmadığını tespit eder."""
        page = self._require_page()
        baseline_text = (getattr(self, "_last_prompt_response", "") or "").strip()

        try:
            resp = (self.read_response_text(latest_only=True) or "").strip()
            if resp and resp != baseline_text:
                lower = resp.lower()
                if any(trig in lower for trig in RATE_LIMIT_TRIGGERS):
                    return None
                for trig in TRANSIENT_ERROR_TRIGGERS:
                    if trig in lower:
                        return resp
        except Exception:
            pass

        try:
            containers = page.locator("model-response, [role='alert'], .error-banner, [data-test-id='model-response-text']")
            if containers.count() > 0:
                item = containers.last
                if item.is_visible():
                    txt = (item.inner_text(timeout=500) or "").strip()
                    if txt and txt != baseline_text:
                        lower = txt.lower()
                        if any(trig in lower for trig in RATE_LIMIT_TRIGGERS):
                            return None
                        for trig in TRANSIENT_ERROR_TRIGGERS:
                            if trig in lower:
                                return txt
        except Exception:
            pass

        return None

    def wait_for_new_video(self, timeout_minutes: int, old_sources: set[str]) -> str:
        page = self._require_page()
        deadline = time.time() + max(1, int(timeout_minutes)) * 60
        check_counter = 0
        baseline_text = (getattr(self, "_last_prompt_response", "") or "").strip()
        # Yeni yanıtın üretilmeye başlanması için başlangıç beklemesi
        page.wait_for_timeout(1000)

        while time.time() < deadline:
            if getattr(self, "is_aborted", False):
                raise InterruptedError("İşlem kullanıcı tarafından durduruldu.")
            check_counter += 1

            # Hızlı yüksek trafik / kapasite doluluğu kontrolü: Hemen yakala ve tekrar deneme için bildir
            traffic_err = self.detect_high_traffic_capacity()
            if traffic_err:
                self.log(f"Gemini video sunucu yoğunluğu tespit edildi: {traffic_err[:160]}…")
                raise HighTrafficCapacityError(f"Gemini video sunucusu yoğunluğu / tam kapasite: {traffic_err.strip()[:180]}")

            # Hızlı video politika uyarısı kontrolü: Hemen yakala, kullanıcıyı/uygulamayı bekletme
            refusal = self.detect_video_policy_refusal()
            if refusal:
                self.log(f"Gemini video politikası uyarısı tespit edildi: {refusal[:160]}…")
                raise VideoPolicyRefusalError(f"Gemini video politikası uyarısı: {refusal.strip()[:180]}")

            # Limit dışı geçici hata/uyarı kontrolü: Hemen yakala ve anında tekrar deneme için bildir
            transient_err = self.detect_transient_error()
            if transient_err:
                self.log(f"Gemini geçici hata uyarısı tespit edildi (limit değil): {transient_err[:160]}…")
                raise TransientGeminiError(f"Gemini geçici hata uyarısı: {transient_err.strip()[:180]}")

            # Modelin hala yazıp yazmadığını veya video üretip üretmediğini kontrol et
            is_busy = False
            try:
                stop_btn = page.locator("button[aria-label*='durdur' i], button[aria-label*='stop' i], .stop-button, [data-test-id='stop-generating-button']")
                thinking = page.locator("thinking-dots-animation, thinking-overlay, [data-test-id*='thinking']")
                if (stop_btn.count() > 0 and stop_btn.first.is_visible()) or (thinking.count() > 0 and thinking.first.is_visible()):
                    is_busy = True
            except Exception:
                pass

            resp = self.read_response_text(latest_only=True)
            if resp and resp.strip() != baseline_text:
                self.check_text_safety_or_limits(resp)

            try:
                videos = page.locator("video")
                for i in range(videos.count()):
                    video = videos.nth(i)
                    src = video.get_attribute("src")
                    if not src:
                        source_tags = video.locator("source")
                        if source_tags.count():
                            src = source_tags.first.get_attribute("src")
                    if not is_new_http_video(src, old_sources):
                        continue
                    try:
                        ready = video.evaluate(
                            "(el) => el.readyState >= 2 && el.videoWidth > 0"
                        )
                    except Exception:
                        ready = False
                    if ready:
                        self.log("Yeni ve oynatılabilir video kaynağı bulundu.")
                        return src
            except (SafetyRefusalError, RateLimitError):
                raise
            except Exception as e:
                self.log(f"Video kontrolü sırasında geçici hata: {e}")

            # Model yanıtını tamamladıysa ve yeni video yoksa (yani video yerine yalnızca metin yazıp bitirdiyse):
            # Veo video render işlemi ortalama 15-45 saniye sürer. Bu süreçte video oynatıcı hazırlanırken
            # erken hata üretilmemeli. Yalnızca model kesin olarak video yerine salt metin önerisi verdiyse
            # (veya en az 35 saniye geçip video öğesi oluşmadıysa) geçici hata olarak değerlendirilir.
            has_video_element = False
            try:
                has_video_element = page.locator("generated-video, video-player, video, [data-test-id*='video']").count() > len(old_sources)
            except Exception:
                pass

            if not is_busy and not has_video_element and resp and resp.strip() != baseline_text:
                lower_resp = resp.lower()
                has_video_indicator = any(w in lower_resp for w in [
                    "your video is ready", "videonuz hazır", "0:00 / 0:10", "0:0", "0:1",
                    "generating your video", "video oluşturuluyor", "generating the requested video",
                    "check back to see when your video is ready",
                ])
                # Açık prompt danışmanlığı / metin yanıtı ifadeleri
                is_prompt_consultation = any(w in lower_resp for w in [
                    "video promptunu aşağıda sunuyorum",
                    "flow video promptu",
                    "veo video promptu",
                    "here is a video prompt",
                    "video prompt proposal",
                ])
                if is_prompt_consultation and check_counter >= 8 and not has_video_indicator:
                    self.log(f"Gemini video üretmek yerine metin/öneri yanıtı verdi: '{resp.strip()[:120]}…'")
                    raise TransientGeminiError(f"Gemini video üretmedi, metin yanıtı verdi: '{resp.strip()[:120]}…'")
                elif check_counter >= 35 and not has_video_indicator and len(resp.strip()) > 30:
                    self.log(f"Gemini video üretmek yerine metin yanıtı verdi ({check_counter}s): '{resp.strip()[:120]}…'")
                    raise TransientGeminiError(f"Gemini video üretmedi, metin yanıtı verdi: '{resp.strip()[:120]}…'")

            page.wait_for_timeout(1000)
        raise TimeoutError(
            f"{timeout_minutes} dakika içinde doğrulanabilir yeni video bulunamadı. "
            "Model isteği tamamlamamış veya arayüz gecikmiş olabilir."
        )

    def read_response_text(self, latest_only: bool = True) -> str | None:
        page = self._require_page()
        selectors = [
            "model-response message-content",
            "model-response .model-response-text",
            "model-response",
            "message-content",
            "[data-test-id='model-response-text']",
            ".model-response-text",
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel)
                count = loc.count()
                if count > 0:
                    if latest_only:
                        text = (loc.last.inner_text(timeout=1000) or "").strip()
                        if text:
                            return text
                    else:
                        chunks = []
                        for i in range(count):
                            t = (loc.nth(i).inner_text(timeout=1000) or "").strip()
                            if t and t not in chunks:
                                chunks.append(t)
                        if chunks:
                            return "\n\n".join(chunks[-2:])
            except Exception:
                continue
        return None

    def _count_model_responses(self) -> int:
        """Sayfadaki model yanıtı (model-response) sayısını döner; yeni yanıtı eski yanıttan ayırmak için kullanılır."""
        try:
            return self._require_page().locator("model-response").count()
        except Exception:
            return 0

    def wait_for_new_response_text(
        self,
        timeout_seconds: int = 30,
        min_scenes: int = 0,
        min_length: int = 0,
    ) -> str | None:
        """Prompt gönderildikten sonra modelin yeni yanıtını bitirmesini bekler ve en son yanıtı döner.
        min_scenes belirtilmişse, model metninde en az o sayıda sahne başlığı bulunana ve metin durulana kadar bekler.
        Maksimum bekleme süresi 30 saniyedir.
        """
        page = self._require_page()
        # Maksimum 30 saniye ile sınırla
        effective_timeout = min(30, max(5, int(timeout_seconds)))
        deadline = time.time() + effective_timeout
        last_text = ""
        stable_count = 0
        baseline_text = getattr(self, "_last_prompt_response", "")

        scene_pat = re.compile(
            r"(?i)(?:\[\s*(?:SAHNE|SCENE)\s*(\d+)[^\]]*\]|(?:^|\n|\r|---|\*\*|#{1,4})\s*(?:SAHNE|SCENE)\s*(\d+)\b[^\n:]*(?::|\-|\n|\*\*))"
        )

        # Modelin yazmaya başlaması için kısa bekleme (1 sn)
        page.wait_for_timeout(1000)

        while time.time() < deadline:
            if getattr(self, "is_aborted", False):
                raise InterruptedError("İşlem kullanıcı tarafından durduruldu.")

            # Modelin hala yazıp yazmadığını (Stop/Durdur butonu veya thinking) kontrol et
            is_busy = False
            try:
                stop_btn = page.locator("button[aria-label*='durdur' i], button[aria-label*='stop' i], .stop-button, [data-test-id='stop-generating-button']")
                thinking = page.locator("thinking-dots-animation, thinking-overlay, [data-test-id*='thinking']")
                if (stop_btn.count() > 0 and stop_btn.first.is_visible()) or (thinking.count() > 0 and thinking.first.is_visible()):
                    is_busy = True
            except Exception:
                pass

            current_text = self.read_response_text(latest_only=True) or ""
            baseline_count = int(getattr(self, "_baseline_response_count", 0) or 0)
            current_count = self._count_model_responses()

            # Geçerli bir aday mı:
            # 1. Metin baseline'dan farklıysa
            # 2. VEYA model yeni bir turn oluşturduysa (current_count > baseline_count)
            # 3. VEYA ekranda sahne planı blokları ([SAHNE 1]) varsa ve model artık meşgul değilse
            has_scene_block = bool(re.search(r"(?i)\[\s*(?:SAHNE|SCENE)\s*1\b", current_text))
            is_valid_candidate = (
                (current_text and current_text != baseline_text)
                or (current_count > baseline_count and bool(current_text))
                or (has_scene_block and not is_busy and len(current_text) > 100)
            )

            if current_text and is_valid_candidate:
                # Güvenlik/limit denetimi yalnızca model yazmayı bitirdiğinde yapılır
                if not is_busy:
                    self.check_text_safety_or_limits(current_text)

                scenes_found = 0
                if min_scenes > 0:
                    scenes_found = len(set(m.group(1) or m.group(2) for m in scene_pat.finditer(current_text)))

                has_enough_scenes = (min_scenes == 0) or (scenes_found >= min_scenes) or (scenes_found > 0 and not is_busy)
                has_enough_length = len(current_text) > max(30, min_length)

                if not is_busy and has_enough_scenes and has_enough_length:
                    if len(current_text) == len(last_text):
                        stable_count += 1
                        if stable_count >= 2:
                            self.log(f"Gemini yanıtı tamamlandı ({len(current_text)} karakter, {scenes_found} sahne).")
                            return current_text
                    else:
                        stable_count = 0
                        last_text = current_text
                else:
                    last_text = current_text
                    stable_count = 0
            page.wait_for_timeout(800)

        if last_text and (last_text != baseline_text or bool(re.search(r"(?i)\[\s*(?:SAHNE|SCENE)\s*1\b", last_text))):
            return last_text
        # Son çare: ekrandaki en son metin geçerli bir sahne planı içeriyorsa doğrudan döndür
        final_text = self.read_response_text(latest_only=True) or ""
        if final_text and re.search(r"(?i)\[\s*(?:SAHNE|SCENE)\s*1\b", final_text):
            return final_text
        return None



    def download_file(self, media_src: str, dest_path: str | Path) -> bool:
        """Video veya görsel URL'sini doğrudan yerel hedefe indirir."""
        context = self._context
        if not context:
            raise ProviderError("Bağlantı açık değil.")
        target = Path(dest_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        response = context.request.get(media_src, timeout=120000)
        if not response.ok:
            raise ProviderError(f"Medya indirilemedi: HTTP {response.status}")
        body = response.body()
        if not body:
            raise ProviderError("İndirilen medya boş.")
        target.write_bytes(body)
        self.log(f"Dosya kaydedildi: {target.name} ({len(body) / 1024 / 1024:.2f} MB)")
        return True

    def _ensure_drive_page(self, goto_root: bool = False):
        context = self._context
        if not context:
            raise ProviderError("Bağlantı açık değil.")
        raw_target = (self.cfg.get("target_folder_id") or self.cfg.get("root_drive_folder") or "").strip()
        target_id = extract_drive_folder_id(raw_target)
        if not target_id:
            raise ProviderError("Hedef Drive klasör ID'si yok; yükleme yapılamaz.")

        target_url = f"https://drive.google.com/drive/folders/{target_id}"
        drive_page = next((pg for pg in context.pages if "drive.google.com" in pg.url), None)
        if drive_page is None:
            drive_page = context.new_page()
            drive_page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            drive_page.wait_for_timeout(2000)
            return drive_page

        if goto_root:
            if target_id not in drive_page.url:
                drive_page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
                drive_page.wait_for_timeout(2000)
        else:
            try:
                drive_page.bring_to_front()
            except Exception:
                pass
            drive_page.wait_for_timeout(300)
        return drive_page

    def create_drive_folder(self, folder_name: str) -> bool:
        """Geçerli Google Drive konumunda yeni bir alt klasör oluşturur."""
        drive_page = self._ensure_drive_page(goto_root=False)
        drive_page.keyboard.press("Escape")
        drive_page.wait_for_timeout(300)

        # "+ Yeni" butonuna tıkla
        new_btn = drive_page.locator('[guidedhelpid="new_menu_button"]').first
        if not new_btn.count() or not new_btn.is_visible():
            new_btn = drive_page.get_by_role("button", name="Yeni", exact=True).or_(
                drive_page.get_by_role("button", name="New", exact=True)
            ).first
        new_btn.click(timeout=4000)
        drive_page.wait_for_timeout(400)

        # "Yeni klasör" seçeneğine tıkla
        new_folder_item = drive_page.locator('[role="menuitem"], .a-c-k').filter(
            has_text=re.compile(r"Yeni klasör|New folder", re.I)
        ).first
        new_folder_item.click(timeout=3000)
        drive_page.wait_for_timeout(600)

        # Klasör adı inputunu doldur ve Oluştur butonuna bas / Enter
        dialog = drive_page.locator('[role="dialog"]').filter(
            has_text=re.compile(r"Yeni klasör|New folder", re.I)
        ).first
        inp = dialog.locator('input[type="text"], input').first
        if not inp.count() or not inp.is_visible():
            inp = drive_page.locator('input[aria-label*="Yeni klasör" i], input[aria-label*="New folder" i]').first

        if inp.is_visible():
            inp.fill(folder_name)
            submit_btn = dialog.get_by_role("button", name=re.compile(r"Oluştur|Create", re.I)).first
            if submit_btn.is_visible():
                submit_btn.click()
            else:
                inp.press("Enter")
            drive_page.wait_for_timeout(2000)
            self.log(f"Google Drive'da klasör oluşturuldu: {folder_name}")
            return True
        return False

    def upload_files_to_current_drive_folder(self, files: list[Path]) -> bool:
        """Geçerli Drive konumuna verilen dosyaları doğrudan tek tek, eksiksiz ve doğrulanmış olarak yükler."""
        valid_files = [f for f in files if f.is_file() and not f.name.startswith(".")]
        if not valid_files:
            return True
        drive_page = self._ensure_drive_page(goto_root=False)

        all_ok = True
        for vf in valid_files:
            drive_page.keyboard.press("Escape")
            drive_page.wait_for_timeout(300)

            # 1. Dosya geçerli klasörde zaten mevcut mu kontrol et
            try:
                existing = drive_page.locator('[data-id], [role="row"], [role="gridcell"]').filter(has_text=vf.name).first
                if existing.is_visible():
                    self.log(f"[Drive] '{vf.name}' zaten mevcut, tekrar yükleme atlandı.")
                    continue
            except Exception:
                pass

            file_size_mb = vf.stat().st_size / (1024 * 1024)
            self.log(f"[Drive] Yükleme başlatılıyor: {vf.name} ({file_size_mb:.2f} MB)…")

            # "+ Yeni" butonunu bul ve tıkla
            new_btn = drive_page.locator('[guidedhelpid="new_menu_button"]').first
            if not new_btn.count() or not new_btn.is_visible():
                new_btn = drive_page.get_by_role("button", name="Yeni", exact=True).or_(
                    drive_page.get_by_role("button", name="New", exact=True)
                ).first

            try:
                new_btn.click(timeout=5000)
                drive_page.wait_for_timeout(400)
            except Exception as e:
                self.log(f"[Drive] '+ Yeni' menü butonu açılamadı: {e}")
                all_ok = False
                continue

            file_item = drive_page.locator('[role="menuitem"], .a-c-k').filter(
                has_text=re.compile(r"Dosya yükle|File upload", re.I)
            ).first

            file_uploaded = False
            try:
                with drive_page.expect_file_chooser(timeout=8000) as fc:
                    file_item.click(timeout=4000)
                chooser = fc.value
                chooser.set_files(str(vf.resolve()))
                self.log(f"[Drive] {vf.name} seçildi, aktarımın tamamlanması bekleniyor…")

                # Boyuta göre dinamik bekleme süresi: taban 45s + her MB için 10s
                timeout_seconds = max(45, int(file_size_mb * 10) + 35)
                deadline = time.time() + timeout_seconds

                while time.time() < deadline:
                    # Klasör içeriğinde dosya göründü mü?
                    try:
                        row = drive_page.locator('[data-id], [role="row"], [role="gridcell"]').filter(has_text=vf.name).first
                        if row.is_visible():
                            file_uploaded = True
                            break
                    except Exception:
                        pass

                    # Sağ alttaki bildirim çekmecesinde dosya adı ve onay var mı?
                    try:
                        toast = drive_page.locator('[aria-label*="tamamlandı" i], [aria-label*="complete" i]').first
                        if toast.is_visible() and vf.name in (toast.inner_text() or ""):
                            file_uploaded = True
                            break
                    except Exception:
                        pass

                    drive_page.wait_for_timeout(1500)

                if file_uploaded:
                    self.log(f"[Drive] ✅ {vf.name} başarıyla Google Drive'a yüklendi.")
                else:
                    self.log(f"[Drive] ⚠️ {vf.name} yükleme zaman aşımına uğradı ({timeout_seconds} sn).")
                    all_ok = False

            except Exception as e:
                self.log(f"[Drive] {vf.name} yüklenirken hata oluştu: {e}")
                all_ok = False

            drive_page.wait_for_timeout(1000)

        return all_ok

    def upload_folder_to_drive(self, folder_path: Path | str) -> dict[str, Any]:
        """Klasörü ve içindeki dosyaları Drive'da hiyerarşik olarak oluşturup yükler."""
        p = Path(folder_path)
        if not p.is_dir():
            raise ProviderError(f"Yüklenecek klasör bulunamadı: {p}")
        sub_scenarios = p / "01_Senaryo"
        sub_images = p / "02_Gorseller"
        sub_videos = p / "03_Videolar"
        if sub_scenarios.exists() or sub_images.exists() or sub_videos.exists():
            return self.upload_all_outputs_to_drive({
                "base": p,
                "scenarios": sub_scenarios,
                "images": sub_images,
                "videos": sub_videos,
            })
        folder_name = p.name
        self.create_drive_folder(folder_name)
        drive_page = self._ensure_drive_page(goto_root=False)
        target_item = drive_page.locator('[data-id], [role="row"], [role="gridcell"]').filter(
            has_text=folder_name
        ).first
        if target_item.is_visible():
            target_item.click()
            drive_page.wait_for_timeout(300)
            drive_page.keyboard.press("Enter")
            drive_page.wait_for_timeout(2000)
            files = [f for f in p.iterdir() if f.is_file() and not f.name.startswith(".")]
            if files:
                self.upload_files_to_current_drive_folder(files)
        return {"folder": folder_name, "verified": True}

    def upload_all_outputs_to_drive(self, dirs: dict[str, Path]) -> dict[str, Any]:
        """Tüm proje çıktılarını (Senaryo, Görseller, Videolar) Google Drive'a hiyerarşik olarak aktarır.
        Ana Drive klasöründe proje klasörünü oluşturur, içine girip 01_Senaryo, 02_Gorseller, 03_Videolar
        klasörlerini açar ve ilgili dosyaları tek tek eksiksiz aktarır.
        """
        base = dirs.get("base")
        if not base or not Path(base).is_dir():
            raise ProviderError("Yüklenecek temel çıktı klasörü bulunamadı.")

        campaign_name = Path(base).name
        self.log(f"Google Drive ana klasörüne proje teslimatı başlatılıyor: {campaign_name}")
        drive_page = self._ensure_drive_page(goto_root=True)

        # 1. Ana klasörde Proje / Kampanya klasörünün varlığını kontrol et veya oluştur
        drive_page.wait_for_timeout(2000)
        target_item = drive_page.locator('[data-id], [role="row"], [role="gridcell"]').filter(
            has_text=campaign_name
        ).first
        if not target_item.is_visible():
            self.create_drive_folder(campaign_name)
            drive_page.wait_for_timeout(2000)
            target_item = drive_page.locator('[data-id], [role="row"], [role="gridcell"]').filter(
                has_text=campaign_name
            ).first

        # 2. Proje klasörüne doğrudan git veya içine gir
        target_id = target_item.get_attribute("data-id") if target_item.is_visible() else None
        if target_id and not target_id.startswith("ucc-"):
            campaign_url = f"https://drive.google.com/drive/folders/{target_id}"
            drive_page.goto(campaign_url, wait_until="domcontentloaded")
            drive_page.wait_for_timeout(2000)
        elif target_item.is_visible():
            target_item.dblclick()
            drive_page.wait_for_timeout(2500)
            campaign_url = drive_page.url
        else:
            raise ProviderError(f"Drive proje klasörü ({campaign_name}) bulunamadı.")

        self.log(f"Drive proje klasörüne girildi: {campaign_name}")

        # 3. Her alt klasörü proje klasörünün İÇİNDE oluştur ve dosyaları tek tek içine aktar
        sub_dirs = [
            ("01_Senaryo", dirs.get("scenarios")),
            ("02_Gorseller", dirs.get("images")),
            ("03_Videolar", dirs.get("videos")),
        ]
        uploaded_folders = []
        overall_ok = True

        for sub_name, local_dir in sub_dirs:
            if not local_dir or not Path(local_dir).is_dir():
                continue
            local_files = [f for f in sorted(Path(local_dir).iterdir()) if f.is_file() and not f.name.startswith(".")]
            if not local_files:
                continue

            # Proje klasörüne dön
            if drive_page.url != campaign_url:
                drive_page.goto(campaign_url, wait_until="domcontentloaded")
                drive_page.wait_for_timeout(2000)

            # Alt klasör mevcut değilse proje klasörünün içinde oluştur
            sub_item = drive_page.locator('[data-id], [role="row"], [role="gridcell"]').filter(
                has_text=sub_name
            ).first
            if not sub_item.is_visible():
                self.create_drive_folder(sub_name)
                drive_page.wait_for_timeout(2000)
                sub_item = drive_page.locator('[data-id], [role="row"], [role="gridcell"]').filter(
                    has_text=sub_name
                ).first

            # Alt klasörün içine git
            sub_id = sub_item.get_attribute("data-id") if sub_item.is_visible() else None
            if sub_id and not sub_id.startswith("ucc-"):
                sub_url = f"https://drive.google.com/drive/folders/{sub_id}"
                drive_page.goto(sub_url, wait_until="domcontentloaded")
                drive_page.wait_for_timeout(2000)
            elif sub_item.is_visible():
                sub_item.dblclick()
                drive_page.wait_for_timeout(2500)

            self.log(f"Drive alt klasörüne girildi: {campaign_name} / {sub_name} ({len(local_files)} dosya yüklenecek)")

            # Dosyaları tek tek yükle ve doğrula
            ok = self.upload_files_to_current_drive_folder(local_files)
            if not ok:
                overall_ok = False
            uploaded_folders.append(sub_name)

        if not overall_ok:
            self.log(f"⚠️ Google Drive teslimatında bazı dosyalar doğrulanamadı: {campaign_name}")
        else:
            self.log(f"✅ Google Drive teslimatı başarıyla tamamlandı: {campaign_name} ({', '.join(uploaded_folders)})")

        return {
            "verified": overall_ok,
            "folder": campaign_name,
            "folders": uploaded_folders,
        }


    def upload_to_drive(self, video_src: str | Path, target_folder_name: str = "") -> dict[str, Any]:
        p = Path(video_src) if isinstance(video_src, (str, Path)) else None
        if p and p.is_dir():
            return self.upload_folder_to_drive(p)

        page = self._require_page()
        context = self._context
        target_id = (self.cfg.get("target_folder_id") or "").strip()
        if not target_id:
            raise ProviderError("Hedef Drive klasör ID'si yok; yükleme yapılmadı.")

        if p and p.is_file():
            data = p.read_bytes()
            filename = p.name
        else:
            response = context.request.get(str(video_src), timeout=120000)
            if not response.ok:
                raise ProviderError(f"Video URL'sinden içerik alınamadı: HTTP {response.status}")
            data = response.body()
            filename = f"video_{time.strftime('%Y%m%d_%H%M%S')}.mp4"

        if not data:
            raise ProviderError("Video içeriği boş geldi.")
        self.log(f"Dosya belleğe alındı ({len(data) / 1024 / 1024:.1f} MB); Drive'a aktarılıyor…")
        drive_page = self._ensure_drive_page()

        drive_page.keyboard.press("Escape")
        drive_page.wait_for_timeout(300)
        new_btn = drive_page.locator('[guidedhelpid="new_menu_button"]').first
        if not new_btn.count() or not new_btn.is_visible():
            new_btn = drive_page.get_by_role("button", name="Yeni", exact=True).or_(
                drive_page.get_by_role("button", name="New", exact=True)
            ).first
        new_btn.click(timeout=3000)
        drive_page.wait_for_timeout(400)

        upload_item = drive_page.locator('[role="menuitem"], .a-c-k').filter(
            has_text=re.compile(r"Dosya yükle|File upload", re.I)
        ).first

        try:
            with drive_page.expect_file_chooser(timeout=6000) as fc:
                upload_item.click(timeout=3000)
            chooser = fc.value

            if p and p.is_file():
                chooser.set_files(str(p.resolve()))
            else:
                chooser.set_files({"name": filename, "mimeType": "video/mp4", "buffer": data})
        except Exception as e:
            raise ProviderError("Drive'a yükleme başlatılamadı: " + str(e)) from e

        verified = False
        try:
            drive_page.get_by_text(filename, exact=False).first.wait_for(timeout=20000)
            self.log("Drive dosya listesinde dosya adı göründü: " + filename)
            verified = True
        except Exception:
            try:
                drive_page.locator('[data-id], [role="row"], [role="gridcell"]').filter(
                    has_text=filename
                ).first.wait_for(timeout=10000)
                self.log("Drive dosya listesinde dosya öğesi teyit edildi: " + filename)
                verified = True
            except Exception:
                try:
                    drive_page.locator("text=/yüklendi|upload complete|completed/i").first.wait_for(timeout=10000)
                    self.log("Drive yükleme bildirimi teyit edildi: " + filename)
                    verified = True
                except Exception:
                    drive_page.wait_for_timeout(3000)
                    self.log("Yükleme başlatıldı; dosya adı arayüzde henüz kesinleşmedi.")
        return {
            "filename": filename,
            "verified": verified,
            "bytes": len(data),
        }

    def close(self) -> None:
        GeminiWebProvider._active_instances.discard(self)
        self._page = None
        self._context = None
        self._browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    def _require_page(self):
        if self._page is None:
            raise ProviderError("Gemini sayfası açık değil. Önce bağlantı kurulmalı.")
        return self._page

    def _get_video_sources(self) -> set[str]:
        page = self._require_page()
        sources: set[str] = set()
        try:
            videos = page.locator("video")
            for i in range(videos.count()):
                video = videos.nth(i)
                src = video.get_attribute("src")
                if not src:
                    source_tags = video.locator("source")
                    if source_tags.count():
                        src = source_tags.first.get_attribute("src")
                if src:
                    sources.add(src)
        except Exception as e:
            self.log(f"Mevcut video kaynakları okunamadı: {e}")
        return sources

    def _get_image_sources(self) -> set[str]:
        page = self._require_page()
        sources: set[str] = set()
        try:
            images = page.locator("img.image.loaded, img.image")
            for i in range(images.count()):
                src = images.nth(i).get_attribute("src")
                if src:
                    sources.add(src)
        except Exception as e:
            self.log(f"Mevcut görsel kaynakları okunamadı: {e}")
        return sources
