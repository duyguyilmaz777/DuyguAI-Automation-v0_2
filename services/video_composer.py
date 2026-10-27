from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable


def find_ffmpeg() -> str | None:
    """Sistemde ffmpeg çalıştırılabilir dosyasını arar."""
    candidates = [
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/usr/bin/ffmpeg",
        shutil.which("ffmpeg"),
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return c
    return None


def merge_scene_videos(
    video_paths: list[Path | str],
    output_path: Path | str,
    log: Callable[[str], None] | None = None,
) -> Path | None:
    """Verilen sahne videolarını FFmpeg ile sırayla tek bir videoda birleştirir."""
    valid_paths = [Path(p) for p in video_paths if Path(p).is_file() and Path(p).stat().st_size > 1000]
    if not valid_paths:
        if log:
            log("Birleştirilecek geçerli video dosyası bulunamadı.")
        return None

    if len(valid_paths) == 1:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(valid_paths[0], out)
        if log:
            log(f"Tek video kopyalandı: {out.name}")
        return out

    ffmpeg_bin = find_ffmpeg()
    if not ffmpeg_bin:
        if log:
            log("UYARI: Sistemde ffmpeg bulunamadı; video birleştirme atlandı.")
        return None

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8", delete=False) as f:
        list_file = Path(f.name)
        for vp in valid_paths:
            safe_p = str(vp.resolve()).replace("'", "'\\''")
            f.write(f"file '{safe_p}'\n")

    try:
        # 1. Hızlı ve kayıpsız birleştirme (Stream copy)
        cmd_copy = [
            ffmpeg_bin,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c",
            "copy",
            str(out),
        ]
        res = subprocess.run(cmd_copy, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode == 0 and out.exists() and out.stat().st_size > 10000:
            if log:
                log(f"FFmpeg birleştirme tamamlandı: {out.name} ({len(valid_paths)} sahne, {out.stat().st_size / 1024 / 1024:.2f} MB)")
            return out

        # 2. Gerekirse yeniden encode ederek birleştirme (Fallback re-encode)
        if log:
            log("FFmpeg stream copy uyuşmadı, yeniden kodlanarak birleştiriliyor…")
        cmd_reencode = [
            ffmpeg_bin,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(out),
        ]
        res2 = subprocess.run(cmd_reencode, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res2.returncode == 0 and out.exists() and out.stat().st_size > 10000:
            if log:
                log(f"FFmpeg birleştirme (re-encode) tamamlandı: {out.name} ({out.stat().st_size / 1024 / 1024:.2f} MB)")
            return out

        if log:
            log(f"FFmpeg birleştirme notu: {res.stderr[:200]}")
        return None
    except Exception as e:
        if log:
            log(f"Video birleştirme sırasında hata oluştu: {e}")
        return None
    finally:
        if list_file.exists():
            try:
                list_file.unlink()
            except Exception:
                pass
