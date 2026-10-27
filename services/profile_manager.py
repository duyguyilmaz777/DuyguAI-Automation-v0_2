from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

KNOWN_GEMS = {
    "af11977bbdce": {
        "name": "Duygu AI Studio",
        "url": "https://gemini.google.com/gem/af11977bbdce",
        "project": "Duygu AI Studio",
        "description": "Ticari ürün reklamı, klinik ve marka tanıtımı üretim Gem'i",
    },
    "8e63671e6329": {
        "name": "İkizler Masal Durağı",
        "url": "https://gemini.google.com/gem/8e63671e6329",
        "project": "İkizler Masal Durağı",
        "description": "3D Pixar çizgi film animasyon ve çocuk masalı üretim Gem'i",
    },
}

STATE_FILE = Path(__file__).resolve().parent.parent / "jobs" / "profiles_state.json"


@dataclass
class GemAccessInfo:
    gem_id: str
    gem_name: str
    gem_url: str
    status: str = "unverified"  # verified, unverified, denied, needs_permission
    last_verified_at: str | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ChromeProfile:
    profile_key: str  # "Default", "Profile 1", vb.
    account_email: str = ""
    display_name: str = ""
    status: str = "available"  # available, rate_limited, needs_attention, unknown
    last_successful_production: str | None = None
    last_error: str | None = None
    last_error_category: str | None = None
    next_allowed_at: str | None = None
    gem_access: dict[str, GemAccessInfo] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    def is_currently_usable(self) -> bool:
        """Profilin şu an aktif bir kota beklemesinde veya müdahale gerektirip gerektirmediğini kontrol eder."""
        if self.status == "needs_attention":
            return False
        if self.status == "rate_limited" and self.next_allowed_at:
            try:
                dt = datetime.fromisoformat(self.next_allowed_at)
                if datetime.now(timezone.utc) < dt:
                    return False
            except Exception:
                pass
        return True


def extract_gem_id_from_url(url: str) -> str:
    """Gem URL'sinden benzersiz Gem kimliğini çıkarır."""
    if not url:
        return ""
    m = re.search(r"gem/([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else ""


def discover_chrome_profiles(custom_dir: Path | str | None = None) -> list[ChromeProfile]:
    """Chrome-Automation-Profile klasöründeki yetkili profilleri şifre/çerez okumadan güvenle keşfeder."""
    profiles: list[ChromeProfile] = []
    base_dir = Path(custom_dir) if custom_dir else Path(os.path.expanduser("~/Chrome-Automation-Profile"))
    local_state_file = base_dir / "Local State"

    if local_state_file.exists():
        try:
            data = json.loads(local_state_file.read_text(encoding="utf-8"))
            info_cache = data.get("profile", {}).get("info_cache", {})
            for key, val in info_cache.items():
                p = ChromeProfile(
                    profile_key=key,
                    account_email=val.get("user_name") or "",
                    display_name=val.get("name") or val.get("gaia_name") or key,
                )
                # Varsayılan Gem erişim listesini başlat
                for gid, ginfo in KNOWN_GEMS.items():
                    p.gem_access[gid] = GemAccessInfo(
                        gem_id=gid,
                        gem_name=ginfo["name"],
                        gem_url=ginfo["url"],
                    )
                profiles.append(p)
        except Exception:
            pass

    if not profiles:
        # Fallback: Varsayılan tek profil
        default_prof = ChromeProfile(profile_key="Default", account_email="varsayilan_hesap", display_name="Varsayılan Oturum")
        for gid, ginfo in KNOWN_GEMS.items():
            default_prof.gem_access[gid] = GemAccessInfo(gem_id=gid, gem_name=ginfo["name"], gem_url=ginfo["url"])
        profiles.append(default_prof)

    return profiles


def verify_active_page_gem_access(page: Any, target_gem_url: str) -> tuple[bool, str]:
    """Tarayıcı sekmesinde Gem'in gerçekten erişilebilir olup olmadığını doğrular."""
    if not page:
        return False, "Tarayıcı sayfası aktif değil."

    current_url = getattr(page, "url", "") or ""
    gem_id = extract_gem_id_from_url(target_gem_url)

    # 1. 404 veya Sorry / bot engeli kontrolü
    if "sorry" in current_url:
        return False, "Google bot koruması (sorry sayfası) tespit edildi."
    if "accounts.google.com" in current_url:
        return False, "Google hesap giriş sayfası açık; oturum kapalı."

    # 2. Sayfa içeriği ve başlık kontrolü
    try:
        content = page.content().lower()
        if "bu gem mevcut değil" in content or "this gem isn't available" in content or "gem bulunamadı" in content:
            return False, f"Gem ({gem_id}) bulunamadı veya bu hesaba erişim izni verilmemiş."
        if "erişim izni isteyin" in content or "request access" in content:
            return False, f"Gem ({gem_id}) için hesap sahibinden paylaşım izni istenmesi gerekiyor."
    except Exception:
        pass

    # 3. URL doğrulaması
    if gem_id and gem_id in current_url:
        return True, f"Gem ({gem_id}) başarıyla doğrulandı ve erişilebilir."

    return True, "Sayfa Gemini oturumuna bağlı."


class ProfileManager:
    """Chrome/Gemini oturum ve Gem erişim yönetim yöneticisi."""

    def __init__(self, state_file: Path | str | None = None, profile_dir: Path | str | None = None):
        self.state_file = Path(state_file) if state_file else STATE_FILE
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.profile_dir = profile_dir
        self.profiles: dict[str, ChromeProfile] = {}
        self.active_profile_key: str = "Default"
        self._load_and_sync()

    def _load_and_sync(self) -> None:
        discovered = discover_chrome_profiles(self.profile_dir)
        for p in discovered:
            self.profiles[p.profile_key] = p

        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                self.active_profile_key = data.get("active_profile_key", "Default")
                saved_profs = data.get("profiles", {})
                for k, pdata in saved_profs.items():
                    if k in self.profiles:
                        p = self.profiles[k]
                        p.status = pdata.get("status", p.status)
                        p.last_successful_production = pdata.get("last_successful_production")
                        p.last_error = pdata.get("last_error")
                        p.last_error_category = pdata.get("last_error_category")
                        p.next_allowed_at = pdata.get("next_allowed_at")
                        # Restore gem access states
                        saved_gems = pdata.get("gem_access", {})
                        for gid, gdict in saved_gems.items():
                            if gid in p.gem_access:
                                p.gem_access[gid].status = gdict.get("status", "unverified")
                                p.gem_access[gid].last_verified_at = gdict.get("last_verified_at")
                                p.gem_access[gid].notes = gdict.get("notes", "")
            except Exception:
                pass

        if self.active_profile_key not in self.profiles and self.profiles:
            self.active_profile_key = next(iter(self.profiles))

    def _save_state(self) -> None:
        try:
            payload = {
                "active_profile_key": self.active_profile_key,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "profiles": {k: p.to_dict() for k, p in self.profiles.items()},
            }
            self.state_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def get_all_profiles(self) -> list[ChromeProfile]:
        return list(self.profiles.values())

    def get_active_profile(self) -> ChromeProfile:
        return self.profiles.get(self.active_profile_key) or ChromeProfile(profile_key="Default")

    def mark_rate_limited(self, profile_key: str, cooldown_seconds: int = 18000) -> None:
        p = self.profiles.get(profile_key)
        if not p:
            return
        now = datetime.now(timezone.utc)
        p.status = "rate_limited"
        p.next_allowed_at = (now + timedelta(seconds=cooldown_seconds)).isoformat()
        p.last_error = "Gemini kullanım/kota sınırına ulaşıldı."
        p.last_error_category = "quota_exceeded"
        self._save_state()

    def mark_profile_success(self, profile_key: str) -> None:
        p = self.profiles.get(profile_key)
        if not p:
            return
        p.status = "available"
        p.last_successful_production = datetime.now(timezone.utc).isoformat()
        p.last_error = None
        p.last_error_category = None
        p.next_allowed_at = None
        self._save_state()

    def mark_needs_attention(self, profile_key: str, reason: str) -> None:
        p = self.profiles.get(profile_key)
        if not p:
            return
        p.status = "needs_attention"
        p.last_error = reason
        self._save_state()

    def check_gem_access(self, profile_key: str, gem_url: str, page: Any = None) -> tuple[bool, str]:
        """Gem erişim kontrol listesini inceler ve gerekirse aktif sayfada doğrular."""
        p = self.profiles.get(profile_key)
        gem_id = extract_gem_id_from_url(gem_url)
        if not gem_id:
            return True, "Özel Gem URL belirtilmemiş veya standart Gemini."

        if page:
            ok, msg = verify_active_page_gem_access(page, gem_url)
            if p and gem_id in p.gem_access:
                g_info = p.gem_access[gem_id]
                g_info.status = "verified" if ok else "denied"
                g_info.last_verified_at = datetime.now(timezone.utc).isoformat()
                g_info.notes = msg
                self._save_state()
            return ok, msg

        # Eğer sayfa yoksa kayıtlı son doğrulama durumunu incele
        if p and gem_id in p.gem_access:
            g_info = p.gem_access[gem_id]
            if g_info.status == "denied":
                return False, f"Bu Gem ({gem_id}) için erişim reddedilmiş: {g_info.notes}"
            if g_info.status == "needs_permission":
                return False, f"Bu Gem ({gem_id}) için hesap sahibinden paylaşım izni istenmeli."

        return True, f"Gem ({gem_id}) yapılandırmada mevcut."
