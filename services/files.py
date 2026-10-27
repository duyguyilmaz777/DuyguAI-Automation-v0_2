from __future__ import annotations

from pathlib import Path

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def scan_reference_images(folder: str | Path, limit: int = 4) -> list[Path]:
    path = Path(folder)
    if not folder or not path.is_dir():
        return []
    found = sorted(
        [p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTS],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    limit = max(1, int(limit))
    return found[:limit]


def unique_existing_images(paths: list[str | Path], limit: int | None = None) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for raw in paths:
        p = Path(raw)
        key = str(p.resolve()) if p.exists() else str(p)
        if key in seen:
            continue
        if p.suffix.lower() not in IMAGE_EXTS:
            continue
        if not p.is_file():
            continue
        seen.add(key)
        result.append(p)
        if limit is not None and len(result) >= max(1, int(limit)):
            break
    return result


def extract_drive_folder_id(value: str | None) -> str:
    """Google Drive URL'sinden veya metin girdisinden klasör ID'sini ayıklar.

    Desteklenen formatlar:
    - https://drive.google.com/drive/folders/1hFZbJ4BiN0QhEMFhOAIS1gEhNAlJZqwy
    - https://drive.google.com/drive/u/0/folders/1hFZbJ4BiN0QhEMFhOAIS1gEhNAlJZqwy
    - https://drive.google.com/drive/folders/1hFZbJ4BiN0QhEMFhOAIS1gEhNAlJZqwy?usp=sharing
    - https://drive.google.com/open?id=1hFZbJ4BiN0QhEMFhOAIS1gEhNAlJZqwy
    - 1hFZbJ4BiN0QhEMFhOAIS1gEhNAlJZqwy
    """
    import re

    if not value:
        return ""
    val = str(value).strip()
    if not val:
        return ""

    folder_match = re.search(r"[/=]folders/([a-zA-Z0-9_-]+)", val)
    if folder_match:
        return folder_match.group(1)

    id_param_match = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", val)
    if id_param_match:
        return id_param_match.group(1)

    if not val.startswith("http://") and not val.startswith("https://") and "/" not in val:
        cleaned = re.sub(r"[^a-zA-Z0-9_-]", "", val)
        return cleaned

    last_segment = val.split("?")[0].rstrip("/").split("/")[-1]
    if re.match(r"^[a-zA-Z0-9_-]{10,}$", last_segment):
        return last_segment

    return val

