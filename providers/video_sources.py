from __future__ import annotations

def is_new_http_video(src: str | None, old_sources: set[str]) -> bool:
    if not src or not isinstance(src, str):
        return False
    if not src.startswith("http"):
        return False
    return src not in old_sources
