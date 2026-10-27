from __future__ import annotations

from typing import Any, Protocol


class ProviderError(RuntimeError):
    pass


class RateLimitError(ProviderError):
    pass


class HighTrafficCapacityError(ProviderError):
    """Gemini sunucularının geçici aşırı yoğunluk veya tam kapasite uyarısı."""
    pass


class TransientGeminiError(ProviderError):
    """Gemini arayüzünde veya model yanıtında limit dışı geçici hata/uyarı (kısa süre sonra tekrar denenebilir)."""
    pass


class SafetyRefusalError(ProviderError):
    pass


class VideoPolicyRefusalError(SafetyRefusalError):
    pass


class SessionExpiredError(ProviderError):
    """Google/Gemini oturumunun sona ermesi veya yeniden oturum açma gereksinimi."""
    pass


class GemAccessDeniedError(ProviderError):
    """Gem'in bu hesapta bulunamaması, 404 dönmesi veya yetki/paylaşım eksikliği."""
    pass


class BrowserCrashedError(ProviderError):
    """Tarayıcının veya CDP bağlantısının beklenmedik şekilde çökmesi/kapanması."""
    pass


class CDPDisconnectError(BrowserCrashedError):
    """CDP bağlantısının kesilmesi veya soketin kapanması."""
    pass


class TemporaryServerError(ProviderError):
    """Geçici sunucu hatası (500, 502, 503 vb.)."""
    pass


class ProductionTimeoutError(ProviderError):
    """Video veya görsel üretim süresinin zaman aşımına uğraması."""
    pass


class FileSaveError(ProviderError):
    """Üretilen dosyanın diske yazılamaması veya doğrulanamaması."""
    pass


class NeedsUserInterventionError(ProviderError):
    """Kullanıcının tarayıcıda manuel işlem yapmasını gerektiren güvenlik/onay durumu."""
    pass



class ProductionProvider(Protocol):
    def connect(self) -> None: ...

    def reset_chat_session(self) -> None: ...

    def snapshot_videos(self) -> set[str]: ...

    def snapshot_images(self) -> set[str]: ...

    def attach_references(self, refs: list[str]) -> None: ...

    def send_prompt(self, prompt: str) -> None: ...

    def wait_for_new_video(self, timeout_minutes: int, old_sources: set[str]) -> str: ...

    def wait_for_new_image(self, timeout_minutes: int, old_sources: set[str], initial_count: int = 0) -> str | None: ...

    def save_latest_generated_image(self, dest_path: str) -> bool: ...

    def get_image_count(self) -> int: ...

    def wait_until_idle(self, timeout_seconds: int = 30) -> None: ...

    def download_file(self, media_src: str, dest_path: str) -> bool: ...

    def read_response_text(self) -> str | None: ...

    def wait_for_new_response_text(self, timeout_seconds: int = 60, min_scenes: int = 0) -> str | None: ...

    def upload_to_drive(self, video_src: str) -> dict[str, Any]: ...

    def upload_folder_to_drive(self, folder_path: str) -> dict[str, Any]: ...

    def upload_all_outputs_to_drive(self, dirs: dict[str, Any]) -> dict[str, Any]: ...

    def close(self) -> None: ...
