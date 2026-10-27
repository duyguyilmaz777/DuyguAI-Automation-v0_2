"""DuyguAI & İkizler Masal Durağı — Web Test & Production Studio Server.

Tek tıkla tarayıcı üzerinden 2 projenin (DuyguAI Studio & İkizler Masal Durağı)
Gemini tarayıcı otomasyonunu, senaryo ayrıştırmasını, görsel ve video üretimini
test edebilmenizi sağlayan yerel web sunucusu ve API köprüsü.
"""
from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from models.job import (
    CONTENT_TYPES,
    DEFAULT_GEM_URLS,
    PROJECT_DUYGU,
    PROJECT_IKIZLER,
    PROJECTS,
    VIDEO_FORMATS,
    JobOrder,
)
from services.files import extract_drive_folder_id
from services.workflow import run_content_workflow

CONFIG_FILE = BASE_DIR / "config.json"
OUTPUTS_DIR = BASE_DIR / "outputs"
WEB_DIR = BASE_DIR / "web"


class JobStateManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.is_running = False
        self.current_job_id = ""
        self.current_project = PROJECT_DUYGU
        self.current_stage = "idle"
        self.logs: list[dict[str, Any]] = []
        self.last_report: dict[str, Any] | None = None
        self.abort_requested = False
        self.active_output_dir = ""

    def start_job(self, project: str, job_id: str, dry_run: bool):
        with self.lock:
            self.is_running = True
            self.current_job_id = job_id
            self.current_project = project
            self.current_stage = "starting"
            self.abort_requested = False
            self.logs = []
            self.last_report = None
            self.active_output_dir = ""
            self.add_log("INFO", f"İş emri başlatıldı ({job_id}) — Proje: {project} | Güvenli Test: {dry_run}")

    def add_log(self, level: str, message: str):
        with self.lock:
            entry = {
                "time": time.strftime("%H:%M:%S"),
                "level": level,
                "msg": message,
            }
            self.logs.append(entry)
            msg_lower = message.lower()
            if "1. aşama" in msg_lower or "senaryo" in msg_lower:
                self.current_stage = "stage_1_script"
            elif "2. aşama" in msg_lower or "görsel" in msg_lower:
                self.current_stage = "stage_2_images"
            elif "3. aşama" in msg_lower or "video" in msg_lower:
                self.current_stage = "stage_3_videos"
            elif "4. aşama" in msg_lower or "arşiv" in msg_lower or "teslimat" in msg_lower:
                self.current_stage = "stage_4_delivery"
            elif "tamamlandı" in msg_lower and "rapor" in msg_lower:
                self.current_stage = "completed"

            if "hedef klasör: outputs/" in msg_lower:
                try:
                    folder_name = message.split("outputs/")[-1].strip()
                    self.active_output_dir = str(OUTPUTS_DIR / folder_name)
                except Exception:
                    pass

    def finish_job(self, success: bool, message: str, report_data: dict[str, Any] | None = None):
        with self.lock:
            self.is_running = False
            self.current_stage = "completed" if success else "failed"
            self.last_report = report_data
            self.add_log("SUCCESS" if success else "ERROR", message)

    def request_abort(self):
        with self.lock:
            self.abort_requested = True
            self.add_log("WARN", "Kullanıcı tarafından durdurma isteği gönderildi…")

    def get_snapshot(self, log_offset: int = 0) -> dict[str, Any]:
        with self.lock:
            return {
                "is_running": self.is_running,
                "job_id": self.current_job_id,
                "project": self.current_project,
                "stage": self.current_stage,
                "active_output_dir": self.active_output_dir,
                "logs": self.logs[log_offset:],
                "total_logs": len(self.logs),
                "last_report": self.last_report,
            }


JOB_MANAGER = JobStateManager()


def load_config() -> dict[str, Any]:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "dry_run": True,
        "selected_project": PROJECT_DUYGU,
        "gem_url_duygu_ai": DEFAULT_GEM_URLS[PROJECT_DUYGU],
        "gem_url_ikizler": DEFAULT_GEM_URLS[PROJECT_IKIZLER],
        "gem_url": DEFAULT_GEM_URLS[PROJECT_DUYGU],
        "chrome_debug_url": "http://127.0.0.1:9222",
        "video_timeout_minutes": 5,
        "max_images": 4,
    }


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def check_chrome_status(debug_url: str = "http://127.0.0.1:9222") -> dict[str, Any]:
    try:
        req = urllib.request.Request(f"{debug_url}/json/version", headers={"User-Agent": "DuyguAI-Studio"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            ver_data = json.loads(resp.read().decode("utf-8"))

        tabs_req = urllib.request.Request(f"{debug_url}/json/list", headers={"User-Agent": "DuyguAI-Studio"})
        with urllib.request.urlopen(tabs_req, timeout=1.5) as resp:
            tabs = json.loads(resp.read().decode("utf-8"))

        gemini_tabs = [
            {"id": t.get("id"), "title": t.get("title"), "url": t.get("url")}
            for t in tabs
            if "gemini.google.com" in (t.get("url") or "")
        ]

        has_ikizler = any("8e63671e6329" in t["url"] for t in gemini_tabs)
        has_duygu = any("af11977bbdce" in t["url"] for t in gemini_tabs)

        return {
            "online": True,
            "browser": ver_data.get("Browser", "Chrome"),
            "gemini_tabs_count": len(gemini_tabs),
            "ikizler_gem_open": has_ikizler,
            "duygu_ai_gem_open": has_duygu,
            "gemini_tabs": gemini_tabs,
        }
    except Exception as exc:
        return {
            "online": False,
            "error": str(exc),
            "gemini_tabs_count": 0,
            "ikizler_gem_open": False,
            "duygu_ai_gem_open": False,
        }


class StudioWebHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def log_message(self, format, *args):
        pass

    def _send_json(self, data: Any, status: int = 200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/api/status":
            cfg = load_config()
            chrome = check_chrome_status(cfg.get("chrome_debug_url", "http://127.0.0.1:9222"))
            offset = int(query.get("offset", ["0"])[0])
            snapshot = JOB_MANAGER.get_snapshot(log_offset=offset)
            self._send_json({
                "chrome": chrome,
                "job": snapshot,
                "config": cfg,
            })
            return

        if path == "/api/config":
            self._send_json(load_config())
            return

        if path == "/api/logs":
            offset = int(query.get("offset", ["0"])[0])
            self._send_json(JOB_MANAGER.get_snapshot(log_offset=offset))
            return

        if path == "/api/presets":
            presets = {
                PROJECT_DUYGU: [
                    {
                        "name": "Nemlendirici Cilt Serumu (Kozmetik UGC)",
                        "brief": "Cildi 72 saat derinlemesine nemlendiren, hyaluronik asit ve pembe gül suyu içeren doğal ışıltı serumu. Genç kadın kullanıcıların sabah bakım rutinine uygun, estetik ve ferahlatıcı ticari ürün sunumu.",
                        "scenario": "Sahne 1: Sabah güneşi altında lavanta ve gül yaprakları arasında serum şişesinin damlalıkla açılması.\nSahne 2: İpeksi bir damlanın avuca damlaması ve hafif ışıltılı dokusu.\nSahne 3: Şişenin minimalist stüdyoda yanında taze su damlalarıyla parıldaması.",
                        "content_type": "ürün reklamı",
                        "video_format": "9:16",
                        "scene_count": 3,
                        "seconds_per_scene": 5,
                        "voiceover": True,
                    },
                    {
                        "name": "Taşınabilir Mini Espresso Makinesi (Teknoloji)",
                        "brief": "Kamp ve ofis için kablosuz, tek tuşla sıcak ve yoğun kremalı espresso hazırlayan kompakt tasarım kahve makinesi. Modern, enerjik ve premium stüdyo sunumu.",
                        "scenario": "",
                        "content_type": "ürün reklamı",
                        "video_format": "9:16",
                        "scene_count": 2,
                        "seconds_per_scene": 5,
                        "voiceover": True,
                    },
                    {
                        "name": "Sporcu Paslanmaz Çelik Shaker (Fitness)",
                        "brief": "Sızdırmaz, çift katmanlı vakumlu mat siyah sporcu matarası ve shaker. Buz gibi içecekleri 24 saat soğuk tutar.",
                        "scenario": "",
                        "content_type": "ürün reklamı",
                        "video_format": "9:16",
                        "scene_count": 2,
                        "seconds_per_scene": 5,
                        "voiceover": False,
                    },
                ],
                PROJECT_IKIZLER: [
                    {
                        "name": "Sevimli Tavşan Bilmecesi (Okul Öncesi Eğitici)",
                        "brief": "Okul öncesi 3-6 yaş çocuklar için sevimli bir hayvan bilmecesi. Atakan ve Kumsal çocuklara neşeyle bilmece sorar, ipuçları verir ve cevabı eğlenceli bir kutlamayla açıklar.",
                        "scenario": "Sahne 1: Atakan ve Kumsal rengarenk oyun odasında el sallar. Atakan: 'Merhaba arkadaşlar! Bilmece zamanı!' der.\nSahne 2: Kumsal meraklı bir yüzle kulağını gösterir: 'Uzun kulakları var, zıp zıp zıplar ve havucu çok sever! Acaba kim?'\nSahne 3: Sihirli kutudan sevimli beyaz bir tavşan çıkar, ikizler neşeyle alkışlar: 'Eveeeet! Bildiniz, bu sevimli bir tavşan!'",
                        "content_type": "çocuk masalı",
                        "video_format": "9:16",
                        "scene_count": 3,
                        "seconds_per_scene": 5,
                        "voiceover": True,
                    },
                    {
                        "name": "Sihirli Renkler & Uçan Balonlar (Eğitici Masal)",
                        "brief": "Okul öncesi çocuklar için ana ve ara renkleri tanıtan masalsı bir keşif. Kırmızı, sarı ve mavi balonların birleşerek gökkuşağı oluşturması.",
                        "scenario": "",
                        "content_type": "çocuk masalı",
                        "video_format": "9:16",
                        "scene_count": 3,
                        "seconds_per_scene": 5,
                        "voiceover": True,
                    },
                    {
                        "name": "İkizlerle Yanardağ Deneyi (Eğlenceli Bilim)",
                        "brief": "Atakan ve Kumsal mutfakta güvenli ve eğlenceli karbonat-sirke köpük yanardağı deneyi yapar. Bilimi sevdiren 3D Pixar çizgi film sahnesi.",
                        "scenario": "",
                        "content_type": "çocuk masalı",
                        "video_format": "9:16",
                        "scene_count": 2,
                        "seconds_per_scene": 5,
                        "voiceover": True,
                    },
                ],
            }
            self._send_json(presets)
            return

        if path == "/api/outputs":
            folders = []
            if OUTPUTS_DIR.exists():
                for item in sorted(OUTPUTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                    if item.is_dir() and not item.name.startswith("."):
                        has_scenario = (item / "01_Senaryo" / "sahne_plani.json").exists()
                        images_count = len(list((item / "02_Gorseller").glob("*.png"))) if (item / "02_Gorseller").exists() else 0
                        videos_count = len(list((item / "03_Videolar").glob("*.mp4"))) if (item / "03_Videolar").exists() else 0
                        folders.append({
                            "name": item.name,
                            "path": str(item),
                            "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(item.stat().st_mtime)),
                            "has_scenario": has_scenario,
                            "images_count": images_count,
                            "videos_count": videos_count,
                        })
            self._send_json(folders)
            return

        if path == "/api/output-detail":
            folder_name = query.get("folder", [""])[0]
            if not folder_name:
                self._send_json({"error": "Folder name required"}, 400)
                return
            target_dir = OUTPUTS_DIR / folder_name
            if not target_dir.exists() or not target_dir.is_dir():
                self._send_json({"error": "Folder not found"}, 404)
                return

            scenarios_dir = target_dir / "01_Senaryo"
            images_dir = target_dir / "02_Gorseller"
            videos_dir = target_dir / "03_Videolar"

            raw_text = ""
            txt_file = scenarios_dir / "tam_senaryo.txt"
            if txt_file.exists():
                raw_text = txt_file.read_text(encoding="utf-8", errors="ignore")

            scenes_json: list[dict[str, Any]] = []
            json_file = scenarios_dir / "sahne_plani.json"
            if json_file.exists():
                try:
                    scenes_json = json.loads(json_file.read_text(encoding="utf-8", errors="ignore"))
                except Exception:
                    pass

            images = []
            if images_dir.exists():
                for img in sorted(images_dir.glob("*.png")):
                    images.append({
                        "name": img.name,
                        "size_kb": round(img.stat().st_size / 1024, 1),
                        "url": f"/api/media?path={urllib.parse.quote(str(img))}",
                    })

            videos = []
            if videos_dir.exists():
                for vid in sorted(videos_dir.glob("*.mp4")):
                    videos.append({
                        "name": vid.name,
                        "size_mb": round(vid.stat().st_size / 1024 / 1024, 2),
                        "url": f"/api/media?path={urllib.parse.quote(str(vid))}",
                        "is_full": vid.name == "tam_video.mp4",
                    })

            insta_text = ""
            insta_file = target_dir / "instagram_paylasim.txt"
            if insta_file.exists():
                insta_text = insta_file.read_text(encoding="utf-8", errors="ignore")

            self._send_json({
                "folder": folder_name,
                "path": str(target_dir),
                "scenario_text": raw_text,
                "scenes": scenes_json,
                "images": images,
                "videos": videos,
                "instagram_package": insta_text,
            })
            return

        if path == "/api/media":
            media_path = query.get("path", [""])[0]
            if not media_path:
                self.send_error(HTTPStatus.BAD_REQUEST, "File path missing")
                return
            file_p = Path(urllib.parse.unquote(media_path))
            try:
                file_p.resolve().relative_to(BASE_DIR.resolve())
            except ValueError:
                self.send_error(HTTPStatus.FORBIDDEN, "Access denied")
                return

            if not file_p.exists() or not file_p.is_file():
                self.send_error(HTTPStatus.NOT_FOUND, "File not found")
                return

            ctype, _ = mimetypes.guess_type(str(file_p))
            if not ctype:
                ctype = "application/octet-stream"

            file_size = file_p.stat().st_size
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(file_size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            with open(file_p, "rb") as f:
                while chunk := f.read(65536):
                    try:
                        self.wfile.write(chunk)
                    except BrokenPipeError:
                        break
            return

        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"

        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            payload = {}

        if path == "/api/config":
            current = load_config()
            current.update(payload)
            save_config(current)
            self._send_json({"ok": True, "config": current})
            return

        if path == "/api/open-folder":
            folder_path = payload.get("path")
            if folder_path and Path(folder_path).exists():
                try:
                    subprocess.run(["open", str(folder_path)], check=False)
                    self._send_json({"ok": True})
                    return
                except Exception as ex:
                    self._send_json({"ok": False, "error": str(ex)}, 500)
                    return
            self._send_json({"ok": False, "error": "Geçersiz klasör"}, 400)
            return

        if path == "/api/stop":
            JOB_MANAGER.request_abort()
            self._send_json({"ok": True, "message": "Durdurma sinyali gönderildi."})
            return

        if path == "/api/run":
            if JOB_MANAGER.is_running:
                self._send_json({"ok": False, "error": "Zaten devam eden bir üretim görevi var!"}, 400)
                return

            project_name = payload.get("project_name") or PROJECT_DUYGU
            dry_run = bool(payload.get("dry_run", True))
            stage_filter = payload.get("stage_filter", "all")

            cfg = load_config()
            cfg.update({
                "project_name": project_name,
                "selected_project": project_name,
                "brief": payload.get("brief", "").strip(),
                "scenario": payload.get("scenario", "").strip(),
                "extra_notes": payload.get("extra_notes", "").strip(),
                "content_type": payload.get("content_type", "ürün reklamı" if project_name == PROJECT_DUYGU else "çocuk masalı"),
                "video_format": payload.get("video_format", "9:16"),
                "scene_count": int(payload.get("scene_count", 2)),
                "seconds_per_scene": int(payload.get("seconds_per_scene", 5)),
                "voiceover": bool(payload.get("voiceover", True)),
                "dry_run": dry_run,
            })

            if project_name == PROJECT_IKIZLER:
                cfg["gem_url"] = cfg.get("gem_url_ikizler", DEFAULT_GEM_URLS[PROJECT_IKIZLER])
            else:
                cfg["gem_url"] = cfg.get("gem_url_duygu_ai", DEFAULT_GEM_URLS[PROJECT_DUYGU])

            if stage_filter == "stage1":
                cfg["target_image_scenes"] = []
                cfg["target_video_scenes"] = []
            elif stage_filter == "stage2":
                cfg["target_video_scenes"] = []

            job_id = f"job_{int(time.time())}"
            cfg["job_id"] = job_id
            save_config(cfg)

            def worker():
                JOB_MANAGER.start_job(project_name, job_id, dry_run)
                try:
                    def web_logger(msg: str):
                        JOB_MANAGER.add_log("INFO", msg)

                    report = run_content_workflow(
                        cfg=cfg,
                        refs=[],
                        log=web_logger,
                    )
                    report_dict = report.to_dict() if hasattr(report, "to_dict") else {}
                    success = bool(getattr(report, "delivered", False) or getattr(report, "video_src_detected", False) or dry_run)
                    JOB_MANAGER.finish_job(
                        success=success,
                        message=f"İş akışı tamamlandı (Durum: {'Başarılı' if success else 'Tamamlandı/Uyarılı'}).",
                        report_data=report_dict,
                    )
                except Exception as exc:
                    JOB_MANAGER.finish_job(
                        success=False,
                        message=f"İş akışında hata oluştu: {exc}",
                    )

            t = threading.Thread(target=worker, daemon=True)
            t.start()

            self._send_json({
                "ok": True,
                "job_id": job_id,
                "project": project_name,
                "dry_run": dry_run,
                "stage_filter": stage_filter,
                "message": f"{project_name} iş akışı başlatıldı.",
            })
            return

        self.send_error(HTTPStatus.NOT_FOUND, "API not found")


def run_server(port: int = 8765):
    WEB_DIR.mkdir(parents=True, exist_ok=True)
    server_address = ("0.0.0.0", port)
    httpd = ThreadingHTTPServer(server_address, StudioWebHandler)
    print(f"\n=======================================================", flush=True)
    print(f"🚀 DuyguAI & İkizler Masal Durağı Web Studio Hazır!", flush=True)
    print(f"👉 Tarayıcınızda açın: http://localhost:{port}", flush=True)
    print(f"=======================================================\n", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nWeb sunucusu kapatılıyor…", flush=True)
        httpd.shutdown()


if __name__ == "__main__":
    port = 8765
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    run_server(port)
