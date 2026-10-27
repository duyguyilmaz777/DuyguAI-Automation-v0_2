from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from providers.base import (
    BrowserCrashedError,
    FileSaveError,
    GemAccessDeniedError,
    HighTrafficCapacityError,
    NeedsUserInterventionError,
    ProductionTimeoutError,
    ProviderError,
    RateLimitError,
    SafetyRefusalError,
    SessionExpiredError,
    TemporaryServerError,
    VideoPolicyRefusalError,
)


class ErrorCategory(str, Enum):
    QUOTA_EXCEEDED = "quota_exceeded"
    HIGH_TRAFFIC_CAPACITY = "high_traffic_capacity"
    TEMPORARY_SERVER_ERROR = "temporary_server_error"
    NETWORK_CDP_DISCONNECTED = "network_cdp_disconnected"
    SESSION_EXPIRED = "session_expired"
    GEM_ACCESS_DENIED = "gem_access_denied"
    PRODUCTION_TIMEOUT = "production_timeout"
    BROWSER_CRASHED = "browser_crashed"
    FILE_SAVE_ERROR = "file_save_error"
    CONTENT_POLICY_REFUSAL = "content_policy_refusal"
    NEEDS_USER_INTERVENTION = "needs_user_intervention"
    UNKNOWN = "unknown"


@dataclass
class ErrorInfo:
    category: ErrorCategory
    raw_message: str
    human_message: str
    is_retryable: bool
    suggested_backoff_seconds: int
    requires_user_intervention: bool
    estimated_reset_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "raw_message": self.raw_message,
            "human_message": self.human_message,
            "is_retryable": self.is_retryable,
            "suggested_backoff_seconds": self.suggested_backoff_seconds,
            "requires_user_intervention": self.requires_user_intervention,
            "estimated_reset_at": self.estimated_reset_at,
        }


# Tanıma anahtar kelimeleri
RATE_LIMIT_PATTERNS = [
    r"limit reached",
    r"usage limit",
    r"kullanım sınırına ulaştınız",
    r"limitine ulaşıldı",
    r"limitine ulaştı",
    r"video limitine",
    r"kota",
    r"daha sonra tekrar deneyin",
    r"quota exceeded",
    r"too many requests",
    r"try again in",
    r"rate limit",
]

HIGH_TRAFFIC_PATTERNS = [
    r"unusually high traffic",
    r"high traffic right now",
    r"generating videos and am at full capacity",
    r"at full capacity",
    r"full capacity",
    r"experiencing unusually high traffic",
    r"yüksek trafik",
    r"tam kapasitede",
    r"aşırı yoğunluk",
    r"sunucu yoğunluğu",
]

SESSION_EXPIRED_PATTERNS = [
    r"sign in",
    r"oturum aç",
    r"giriş yap",
    r"session expired",
    r"accounts\.google\.com",
    r"authentication required",
    r"kimlik doğrulama",
]

GEM_ACCESS_PATTERNS = [
    r"gem not found",
    r"gem bulunamadı",
    r"access denied",
    r"erişim reddedildi",
    r"yetkiniz yok",
    r"404",
    r"gemini\.google\.com/sorry",
    r"this gem isn't available",
    r"bu gem mevcut değil",
]

CDP_DISCONNECT_PATTERNS = [
    r"target closed",
    r"connection refused",
    r"connect econnrefused",
    r"websocket",
    r"cannot connect to chrome",
    r"cdp bağlantısı",
    r"browser has been closed",
]

POLICY_REFUSAL_PATTERNS = [
    r"video policy",
    r"policy guidelines",
    r"politika kuralları",
    r"can't generate that video",
    r"bu videoyu oluşturamıyorum",
    r"safety guidelines",
]


def classify_error(
    exc: Exception | str,
    context_text: str | None = None,
    cfg: dict[str, Any] | None = None,
) -> ErrorInfo:
    """Oluşan hatayı analiz eder ve uygun kurtarma/bekleme stratejisini belirler."""
    cfg = cfg or {}
    raw_str = f"{exc} {context_text or ''}".strip()
    lower_str = raw_str.lower()

    # 1. Kota ve Kullanım Sınırı (Kesin süre bilinmiyorsa tahmini olarak kaydedilir)
    if isinstance(exc, RateLimitError) or any(re.search(p, lower_str) for p in RATE_LIMIT_PATTERNS):
        cooldown_min = int(cfg.get("quota_cooldown_minutes") or 300)
        reset_time = (datetime.now(timezone.utc) + timedelta(minutes=cooldown_min)).isoformat()
        return ErrorInfo(
            category=ErrorCategory.QUOTA_EXCEEDED,
            raw_message=raw_str,
            human_message=f"Gemini kullanım limitine / kotasına ulaşıldı. Tahmini bekleme süresi: {cooldown_min} dakika. "
                          f"Süre dolduğunda iş güvenli şekilde kaldığı yerden devam edecektir.",
            is_retryable=True,
            suggested_backoff_seconds=cooldown_min * 60,
            requires_user_intervention=False,
            estimated_reset_at=reset_time,
        )

    # 2. Geçici Sunucu Yoğunluğu (Veo / Video Kapasitesi)
    if isinstance(exc, HighTrafficCapacityError) or any(re.search(p, lower_str) for p in HIGH_TRAFFIC_PATTERNS):
        backoff_sec = int(cfg.get("high_traffic_retry_seconds") or 25)
        return ErrorInfo(
            category=ErrorCategory.HIGH_TRAFFIC_CAPACITY,
            raw_message=raw_str,
            human_message="Gemini video üretim sunucularında geçici aşırı yoğunluk/kapasite durumu var. "
                          f"{backoff_sec} saniye sonra otomatik tekrar denenecek.",
            is_retryable=True,
            suggested_backoff_seconds=backoff_sec,
            requires_user_intervention=False,
        )

    # 3. Geçici Sunucu Hatası (500, 502, 503)
    if isinstance(exc, TemporaryServerError) or any(w in lower_str for w in ["503", "502", "500", "geçici yanıt veremiyor", "temporary server"]):
        return ErrorInfo(
            category=ErrorCategory.TEMPORARY_SERVER_ERROR,
            raw_message=raw_str,
            human_message="Sunucu geçici hata döndürdü (500/502/503). Güvenli aralıkla tekrar denenecektir.",
            is_retryable=True,
            suggested_backoff_seconds=30,
            requires_user_intervention=False,
        )

    # 3. Oturum Sona Ermesi / Yeniden Giriş
    if isinstance(exc, SessionExpiredError) or any(re.search(p, lower_str) for p in SESSION_EXPIRED_PATTERNS):
        return ErrorInfo(
            category=ErrorCategory.SESSION_EXPIRED,
            raw_message=raw_str,
            human_message="Gemini oturumunun süresi dolmuş veya Google hesabı yeniden giriş istiyor. "
                          "Lütfen Chrome'da oturumu açın.",
            is_retryable=False,
            suggested_backoff_seconds=0,
            requires_user_intervention=True,
        )

    # 4. Gem Erişim Engeli / Paylaşım Eksikliği
    if isinstance(exc, GemAccessDeniedError) or any(re.search(p, lower_str) for p in GEM_ACCESS_PATTERNS):
        return ErrorInfo(
            category=ErrorCategory.GEM_ACCESS_DENIED,
            raw_message=raw_str,
            human_message="Seçilen Gemini Gem'ine bu hesaptan erişilemiyor veya Gem paylaşılmamış. "
                          "Gem bağlantısını ve yetkilerini kontrol ediniz.",
            is_retryable=False,
            suggested_backoff_seconds=0,
            requires_user_intervention=True,
        )

    # 5. Tarayıcı Çökmesi veya CDP Bağlantı Kesintisi
    if isinstance(exc, BrowserCrashedError) or any(re.search(p, lower_str) for p in CDP_DISCONNECT_PATTERNS):
        return ErrorInfo(
            category=ErrorCategory.NETWORK_CDP_DISCONNECTED,
            raw_message=raw_str,
            human_message="Chrome CDP bağlantısı kesildi veya tarayıcı penceresi kapandı. "
                          "Bağlantı kontrol edilip yeniden denenecektir.",
            is_retryable=True,
            suggested_backoff_seconds=15,
            requires_user_intervention=False,
        )

    # 6. Politika / Güvenlik Reddi
    if isinstance(exc, (VideoPolicyRefusalError, SafetyRefusalError)) or any(re.search(p, lower_str) for p in POLICY_REFUSAL_PATTERNS):
        return ErrorInfo(
            category=ErrorCategory.CONTENT_POLICY_REFUSAL,
            raw_message=raw_str,
            human_message="Gemini içerik/video güvenlik politikasından dolayı üretim talebini reddetti. "
                          "Prompt veya referans görsel müdahalesi gereklidir.",
            is_retryable=False,
            suggested_backoff_seconds=0,
            requires_user_intervention=True,
        )

    # 7. Üretim Zaman Aşımı
    if isinstance(exc, (ProductionTimeoutError, TimeoutError)):
        return ErrorInfo(
            category=ErrorCategory.PRODUCTION_TIMEOUT,
            raw_message=raw_str,
            human_message="Video/görsel üretim süresi zaman aşımına uğradı. Çıktı klasörü kontrol edilip tekrar denenebilir.",
            is_retryable=True,
            suggested_backoff_seconds=30,
            requires_user_intervention=False,
        )

    # 8. Dosya Kaydetme Hatası
    if isinstance(exc, (FileSaveError, OSError, IOError)):
        return ErrorInfo(
            category=ErrorCategory.FILE_SAVE_ERROR,
            raw_message=raw_str,
            human_message="Üretilen dosya diske kaydedilirken hata oluştu. Disk alanını ve izinleri kontrol ediniz.",
            is_retryable=False,
            suggested_backoff_seconds=0,
            requires_user_intervention=True,
        )

    # 9. Kullanıcı Müdahalesi Gerektiren Durum
    if isinstance(exc, NeedsUserInterventionError):
        return ErrorInfo(
            category=ErrorCategory.NEEDS_USER_INTERVENTION,
            raw_message=raw_str,
            human_message=str(exc) or "Kullanıcı müdahalesi gerektiren bir durum tespit edildi.",
            is_retryable=False,
            suggested_backoff_seconds=0,
            requires_user_intervention=True,
        )

    # Varsayılan: Bilinmeyen / Genel Hata
    return ErrorInfo(
        category=ErrorCategory.UNKNOWN,
        raw_message=raw_str,
        human_message=f"Beklenmeyen bir hata oluştu: {raw_str[:200]}",
        is_retryable=False,
        suggested_backoff_seconds=0,
        requires_user_intervention=True,
    )
