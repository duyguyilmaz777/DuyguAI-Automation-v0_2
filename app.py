import json, os, threading, queue, subprocess
from datetime import datetime, timezone
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from automation import Automation
from models.job import (
    CONTENT_TYPES,
    DEFAULT_GEM_URLS,
    PROJECT_DUYGU,
    PROJECT_IKIZLER,
    PROJECTS,
    VIDEO_FORMATS,
)
from services.files import extract_drive_folder_id, scan_reference_images, unique_existing_images
from services.job_queue import JobQueue
from services.logging_utils import SafeLogger
from services.profile_manager import KNOWN_GEMS, ProfileManager, extract_gem_id_from_url
from services.queue_runner import QueueRunner
from services.validation import validate_job_inputs
from services.workflow import create_job_order

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
LOGS_DIR = APP_DIR / "logs"
JOBS_DIR = APP_DIR / "jobs"
OUTPUTS_DIR = APP_DIR / "outputs"
PENDING_JOB_FILE = JOBS_DIR / "pending_job.json"


def get_pending_job_file(project_name: str | None = None) -> Path:
    """Belirtilen proje için özel taslak dosya yolunu döner."""
    if project_name == PROJECT_IKIZLER:
        return JOBS_DIR / "pending_job_ikizler.json"
    if project_name == PROJECT_DUYGU:
        return JOBS_DIR / "pending_job_duygu_ai.json"
    return JOBS_DIR / "pending_job.json"

SYSTEM_KEYS = {
    "gem_url",
    "gem_url_duygu_ai",
    "gem_url_ikizler",
    "selected_project",
    "root_drive_folder",
    "target_folder_id",
    "source_folder",
    "chrome_debug_url",
    "chrome_app",
    "max_images",
    "video_timeout_minutes",
    "connect_timeout_ms",
    "retry_limit",
    "dry_run",
}


def load_config():
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    if not cfg.get("root_drive_folder") and not cfg.get("target_folder_id"):
        cfg["root_drive_folder"] = "1vANxhcxl0C65igPzq6tuATygMpwNPE_F"
    if not cfg.get("gem_url_duygu_ai"):
        cfg["gem_url_duygu_ai"] = DEFAULT_GEM_URLS[PROJECT_DUYGU]
    if not cfg.get("gem_url_ikizler"):
        cfg["gem_url_ikizler"] = DEFAULT_GEM_URLS[PROJECT_IKIZLER]
    if not cfg.get("selected_project"):
        cfg["selected_project"] = PROJECT_DUYGU
    active_proj = cfg.get("selected_project", PROJECT_DUYGU)
    if active_proj == PROJECT_IKIZLER:
        cfg["gem_url"] = cfg.get("gem_url_ikizler", DEFAULT_GEM_URLS[PROJECT_IKIZLER])
    else:
        cfg["gem_url"] = cfg.get("gem_url_duygu_ai", DEFAULT_GEM_URLS[PROJECT_DUYGU])
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "evet"}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("DuyguAI Automation — İçerik Üretimi")
        self.geometry("980x760")
        self.minsize(820, 580)
        self.cfg = load_config()
        self.current_project = self.cfg.get("selected_project", PROJECT_DUYGU)
        self.log_queue = queue.Queue()
        self.worker = None
        self._running = False
        self.selected_refs: list[Path] = []
        self.regen_img_vars: dict[int, tk.BooleanVar] = {}
        self.regen_vid_vars: dict[int, tk.BooleanVar] = {}
        self.review_img_text_widgets: dict[int, tk.Text] = {}
        self.review_vid_text_widgets: dict[int, tk.Text] = {}
        self.review_folder_var = tk.StringVar()
        self.review_folder_combo = None
        self.review_cards_frame = None
        self.review_info_lbl = None
        self.review_canvas = None
        self.review_regen_btn = None
        self.review_stop_btn = None
        self.review_merge_btn = None
        self.stop_btn = None
        self.review_current_scenes: list[dict] = []
        self.job_queue = JobQueue()
        self.profile_manager = ProfileManager()
        self.queue_runner = QueueRunner(
            job_queue=self.job_queue,
            profile_manager=self.profile_manager,
            log_callback=self.log,
            config=self.cfg,
        )
        self._build()
        self._load_fields()
        self.after(150, self._drain_log)
        self.after(350, self.prompt_project_selection)
        self.after(2000, self._periodic_queue_refresh)

    def _build(self):
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=8, pady=8)
        self.main_tab = ttk.Frame(self.tabs, padding=8)
        self.review_tab = ttk.Frame(self.tabs, padding=8)
        self.queue_tab = ttk.Frame(self.tabs, padding=8)
        self.settings_tab = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(self.main_tab, text="İçerik üretimi")
        self.tabs.add(self.review_tab, text="🎬 Sahne Kontrolü & Tekrar Üretim")
        self.tabs.add(self.queue_tab, text="📋 İş Kuyruğu & Durum")
        self.tabs.add(self.settings_tab, text="Ayarlar")
        self._build_queue_tab()
        self.tabs.bind("<<NotebookTabChanged>>", self._on_notebook_tab_changed)

        # -------------------------------------------------------------
        # 1. Sabit Alt Eylem Çubuğu (Pinned Bottom Action Bar)
        # Her zaman görünür; pencere boyutu ne olursa olsun aşağı kaybolmaz!
        # -------------------------------------------------------------
        actions = ttk.Frame(self.main_tab, padding=(0, 6, 0, 0))
        actions.pack(side="bottom", fill="x")

        self.chrome_btn = ttk.Button(actions, text="🌐 Chrome'u Başlat", command=self.launch_chrome)
        self.chrome_btn.pack(side="left", padx=(0, 6), pady=4)

        self.connect_btn = ttk.Button(actions, text="🔗 Bağlantıyı Test Et", command=self.test_connection)
        self.connect_btn.pack(side="left", padx=(0, 6), pady=4)

        self.run_btn = ttk.Button(
            actions,
            text="🚀 Üretimi Başlat (Tam Otomatik)",
            command=self.start_run,
        )
        self.run_btn.pack(side="left", padx=(0, 6), pady=4)

        self.stop_btn = ttk.Button(
            actions,
            text="🛑 Üretimi Durdur",
            command=self.stop_production,
            state="disabled",
        )
        self.stop_btn.pack(side="left", padx=(0, 6), pady=4)

        self.reset_btn = ttk.Button(actions, text="🔄 Formu Sıfırla / Yeni İş", command=self.confirm_reset_form)
        self.reset_btn.pack(side="left", padx=(0, 6), pady=4)

        self.open_out_btn = ttk.Button(actions, text="📁 Çıktı Klasörünü Aç", command=self.open_outputs)
        self.open_out_btn.pack(side="left", padx=(0, 6), pady=4)

        self.review_btn = ttk.Button(actions, text="🎬 Sahne Kontrolü", command=self.switch_to_review_tab)
        self.review_btn.pack(side="left", pady=4)

        # -------------------------------------------------------------
        # 2. İşlem Günlüğü (Log) - Alt Eylem Çubuğunun Hemen Üzerinde Sabit
        # -------------------------------------------------------------
        log_frame = ttk.LabelFrame(self.main_tab, text="İşlem Günlüğü", padding=6)
        log_frame.pack(side="bottom", fill="x", pady=(6, 2))
        self.log_text = tk.Text(log_frame, height=5, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True)

        # -------------------------------------------------------------
        # 3. Kaydırılabilir İçerik Alanı (Canvas + Scrollbar)
        # Geri kalan tüm alanı kaplar; Mac trackpad & fare tekerleği ile kaydırılır
        # -------------------------------------------------------------
        scroll_container = ttk.Frame(self.main_tab)
        scroll_container.pack(side="top", fill="both", expand=True)

        canvas = tk.Canvas(scroll_container, borderwidth=0, highlightthickness=0)
        v_scroll = ttk.Scrollbar(scroll_container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=v_scroll.set)

        v_scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        scrollable_frame = ttk.Frame(canvas)
        canvas_window = canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")

        def _on_frame_configure(event):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event):
            canvas.itemconfig(canvas_window, width=event.width)

        scrollable_frame.bind("<Configure>", _on_frame_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        def _on_mousewheel(event):
            if event.delta:
                canvas.yview_scroll(int(-1 * event.delta), "units")

        def _bind_mousewheel(event):
            canvas.bind_all("<MouseWheel>", _on_mousewheel)

        def _unbind_mousewheel(event):
            canvas.unbind_all("<MouseWheel>")

        canvas.bind("<Enter>", _bind_mousewheel)
        canvas.bind("<Leave>", _unbind_mousewheel)

        # -------------------------------------------------------------
        # Proje Seçici Üst Bar (Her zaman görünür, hızlı geçiş sağlar)
        # -------------------------------------------------------------
        proj_bar = ttk.LabelFrame(scrollable_frame, text="Aktif Çalışma Projesi", padding=(10, 8))
        proj_bar.pack(fill="x", pady=(0, 10))

        self.project_status_label = ttk.Label(
            proj_bar,
            text="",
            font=("Helvetica", 12, "bold"),
            foreground="#1565c0",
        )
        self.project_status_label.pack(side="left", padx=(4, 12))

        ttk.Button(
            proj_bar,
            text="Projeyi Değiştir...",
            command=lambda: self.prompt_project_selection(force=True),
        ).pack(side="right", padx=4)

        ttk.Button(
            proj_bar,
            text="🧚‍♀️ İkizler Masal Durağı",
            command=lambda: self.switch_project(PROJECT_IKIZLER),
        ).pack(side="right", padx=4)

        ttk.Button(
            proj_bar,
            text="🎬 Duygu AI Studio",
            command=lambda: self.switch_project(PROJECT_DUYGU),
        ).pack(side="right", padx=4)

        # -------------------------------------------------------------
        # 1. Duygu AI Studio Form Çerçevesi (Mevcut yapı ve kategoriler aynen korunur)
        # -------------------------------------------------------------
        self.duygu_form_frame = ttk.Frame(scrollable_frame)

        ttk.Label(
            self.duygu_form_frame,
            text="DuyguAI Studio — Müşteri İçerik Paneli",
            font=("Helvetica", 16, "bold"),
        ).pack(anchor="w", pady=(0, 2))
        ttk.Label(
            self.duygu_form_frame,
            text="Brief, senaryo veya ek notlardan en az biri gerekir. Referans görsel isteğe bağlıdır. "
                 "Güvenli test (dry_run) açıkken Gemini'ye üretim isteği ve Drive yüklemesi yapılmaz.",
            wraplength=900,
        ).pack(anchor="w", pady=(2, 8))

        form = ttk.Frame(self.duygu_form_frame)
        form.pack(fill="x")

        ttk.Label(form, text="İçerik briefi / açıklama (boş bırakılırsa uyarı verilir)").grid(
            row=0, column=0, columnspan=4, sticky="w"
        )
        self.brief_text = tk.Text(form, height=4, wrap="word")
        self.brief_text.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(2, 6))

        ttk.Label(form, text="Hazır senaryo (isteğe bağlı)").grid(row=2, column=0, columnspan=4, sticky="w")
        self.scenario_text = tk.Text(form, height=3, wrap="word")
        self.scenario_text.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(2, 6))

        ttk.Label(form, text="İçerik türü").grid(row=4, column=0, sticky="w")
        self.content_type = ttk.Combobox(form, values=CONTENT_TYPES, state="readonly", width=24)
        self.content_type.grid(row=4, column=1, sticky="w", padx=(8, 16), pady=4)

        ttk.Label(form, text="Video formatı").grid(row=4, column=2, sticky="w")
        self.video_format = ttk.Combobox(form, values=VIDEO_FORMATS, state="readonly", width=10)
        self.video_format.grid(row=4, column=3, sticky="w", padx=8, pady=4)

        ttk.Label(form, text="Sahne sayısı").grid(row=5, column=0, sticky="w")
        self.scene_count = tk.Spinbox(form, from_=1, to=20, width=8)
        self.scene_count.grid(row=5, column=1, sticky="w", padx=(8, 16), pady=4)

        ttk.Label(form, text="Sahne süresi (sn)").grid(row=5, column=2, sticky="w")
        self.seconds_per_scene = tk.Spinbox(form, from_=1, to=60, width=8)
        self.seconds_per_scene.grid(row=5, column=3, sticky="w", padx=8, pady=4)

        self.voiceover_var = tk.BooleanVar()
        self.music_var = tk.BooleanVar()
        self.dry_run_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(form, text="Türkçe seslendirme iste", variable=self.voiceover_var).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=4
        )
        ttk.Checkbutton(form, text="Müzik iste", variable=self.music_var).grid(
            row=6, column=2, columnspan=2, sticky="w", pady=4
        )
        ttk.Checkbutton(
            form,
            text="Güvenli test (dry_run) — üretim isteği ve Drive yüklemesi yok",
            variable=self.dry_run_var,
        ).grid(row=7, column=0, columnspan=4, sticky="w", pady=(2, 6))

        ttk.Label(form, text="Kurgu tercihleri (isteğe bağlı)").grid(row=8, column=0, columnspan=4, sticky="w")
        self.editing_text = tk.Text(form, height=2, wrap="word")
        self.editing_text.grid(row=9, column=0, columnspan=4, sticky="ew", pady=(2, 6))

        ttk.Label(form, text="Ek üretim notları (isteğe bağlı; eski prompt alanı)").grid(
            row=10, column=0, columnspan=4, sticky="w"
        )
        self.extra_text = tk.Text(form, height=2, wrap="word")
        self.extra_text.grid(row=11, column=0, columnspan=4, sticky="ew", pady=(2, 6))

        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)

        # -------------------------------------------------------------
        # 2. İkizler Masal Durağı Form Çerçevesi
        # -------------------------------------------------------------
        self.ikizler_form_frame = ttk.Frame(scrollable_frame)

        ttk.Label(
            self.ikizler_form_frame,
            text="🧚‍♀️ İkizler Masal Durağı — Masal & Çocuk İçerikleri",
            font=("Helvetica", 16, "bold"),
        ).pack(anchor="w", pady=(0, 2))
        ttk.Label(
            self.ikizler_form_frame,
            text="Okul öncesi ve çocuk masalları için masal konusu, yaş grubu, karakterler ve eğitici sahne akışı paneli.\n"
                 "Karakter ve masal referans görselleri eklenebilir; Gemini 3D Pixar animasyon tarzında bu karakterlere sadık kalır.",
            wraplength=900,
        ).pack(anchor="w", pady=(2, 8))

        m_form = ttk.Frame(self.ikizler_form_frame)
        m_form.pack(fill="x")

        ttk.Label(m_form, text="Masal konusu / teması (Örn: Ormandaki Gizemli Dostluk)").grid(
            row=0, column=0, columnspan=4, sticky="w"
        )
        self.masal_topic_entry = ttk.Entry(m_form, width=70)
        self.masal_topic_entry.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(2, 6))

        ttk.Label(m_form, text="Hedef yaş grubu").grid(row=2, column=0, sticky="w")
        self.masal_age_combo = ttk.Combobox(
            m_form,
            values=["2-4 Yaş (Okul Öncesi)", "5-7 Yaş", "8-10 Yaş", "Tüm Çocuklar"],
            state="readonly",
            width=24,
        )
        self.masal_age_combo.set("2-4 Yaş (Okul Öncesi)")
        self.masal_age_combo.grid(row=2, column=1, sticky="w", padx=(8, 16), pady=4)

        ttk.Label(m_form, text="Video formatı").grid(row=2, column=2, sticky="w")
        self.ikizler_video_format = ttk.Combobox(m_form, values=VIDEO_FORMATS, state="readonly", width=10)
        self.ikizler_video_format.set(VIDEO_FORMATS[0])
        self.ikizler_video_format.grid(row=2, column=3, sticky="w", padx=8, pady=4)

        ttk.Label(m_form, text="Ana karakterler (Örn: Minik Tavşan Pamuk, Bilge Baykuş)").grid(
            row=3, column=0, columnspan=4, sticky="w"
        )
        self.masal_chars_entry = ttk.Entry(m_form, width=70)
        self.masal_chars_entry.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(2, 4))

        char_ref_bar = ttk.Frame(m_form)
        char_ref_bar.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(0, 6))
        ttk.Button(char_ref_bar, text="🎨 Karakter Görseli Ekle...", command=self.add_ref_files).pack(side="left", padx=(0, 8))
        ttk.Button(char_ref_bar, text="📂 Klasörden Tara", command=self.scan_refs).pack(side="left", padx=(0, 8))
        self.ikizler_ref_status_lbl = ttk.Label(
            char_ref_bar,
            text="",
            foreground="#2e7d32",
            font=("Helvetica", 11, "bold"),
        )
        self.ikizler_ref_status_lbl.pack(side="left", padx=6)

        ttk.Label(m_form, text="Eğitsel hedef / ana mesaj (Örn: Paylaşmanın güzelliği, empati, doğa sevgisi)").grid(
            row=6, column=0, columnspan=4, sticky="w"
        )
        self.masal_goal_entry = ttk.Entry(m_form, width=70)
        self.masal_goal_entry.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(2, 6))

        ttk.Label(m_form, text="Masal akışı / sahne taslağı (isteğe bağlı hazır metin)").grid(
            row=8, column=0, columnspan=4, sticky="w"
        )
        self.masal_story_text = tk.Text(m_form, height=4, wrap="word")
        self.masal_story_text.grid(row=9, column=0, columnspan=4, sticky="ew", pady=(2, 6))

        ttk.Label(m_form, text="Sahne sayısı").grid(row=10, column=0, sticky="w")
        self.ikizler_scene_count = tk.Spinbox(m_form, from_=1, to=20, width=8)
        self.ikizler_scene_count.delete(0, "end")
        self.ikizler_scene_count.insert(0, "3")
        self.ikizler_scene_count.grid(row=10, column=1, sticky="w", padx=(8, 16), pady=4)

        ttk.Label(m_form, text="Sahne süresi (sn)").grid(row=10, column=2, sticky="w")
        self.ikizler_seconds_per_scene = tk.Spinbox(m_form, from_=1, to=60, width=8)
        self.ikizler_seconds_per_scene.delete(0, "end")
        self.ikizler_seconds_per_scene.insert(0, "5")
        self.ikizler_seconds_per_scene.grid(row=10, column=3, sticky="w", padx=8, pady=4)

        self.ikizler_voiceover_var = tk.BooleanVar(value=True)
        self.ikizler_music_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(m_form, text="Masal seslendirmesi iste (Türkçe)", variable=self.ikizler_voiceover_var).grid(
            row=11, column=0, columnspan=2, sticky="w", pady=4
        )
        ttk.Checkbutton(m_form, text="Çocuk melodisi / arka plan müziği iste", variable=self.ikizler_music_var).grid(
            row=11, column=2, columnspan=2, sticky="w", pady=4
        )
        ttk.Checkbutton(
            m_form,
            text="Güvenli test (dry_run) — üretim isteği ve Drive yüklemesi yok",
            variable=self.dry_run_var,
        ).grid(row=12, column=0, columnspan=4, sticky="w", pady=(2, 6))

        ttk.Label(m_form, text="Ek masal ve prodüksiyon notları (isteğe bağlı)").grid(
            row=13, column=0, columnspan=4, sticky="w"
        )
        self.masal_notes_text = tk.Text(m_form, height=2, wrap="word")
        self.masal_notes_text.grid(row=14, column=0, columnspan=4, sticky="ew", pady=(2, 6))

        m_info = ttk.LabelFrame(m_form, text="Bilgilendirme & Üretim", padding=8)
        m_info.grid(row=15, column=0, columnspan=4, sticky="ew", pady=(6, 4))
        ttk.Label(
            m_info,
            text="ℹ️ İkizler Masal Durağı için özel Gemini Gem'i etkindir: https://gemini.google.com/gem/8e63671e6329\n"
                 "✨ Referans görsel eklendiğinde karakterin yüz, giysi ve tasarımına sadık kalınarak 3D Pixar sahneleri üretilir. "
                 "'Üretimi Başlat' butonuna bastığınızda masal senaryosu, görsel/video üretimi, birleştirme ve Drive arşivlemesi tam otomatik çalışır.",
            wraplength=850,
            foreground="#2e7d32",
        ).pack(anchor="w")

        m_form.columnconfigure(1, weight=1)
        m_form.columnconfigure(3, weight=1)

        # -------------------------------------------------------------
        # 3. Ortak Alanlar: Referans Görselleri ve Google Drive
        # -------------------------------------------------------------
        self.refs_box = ttk.LabelFrame(scrollable_frame, text="Referans Görselleri (İsteğe Bağlı)", padding=(10, 8))
        self.refs_box.pack(fill="x", pady=(4, 6))

        self.refs_hint_label = ttk.Label(
            self.refs_box,
            text="Referans görselleri isteğe bağlıdır.",
            wraplength=850,
            font=("Helvetica", 11),
        )
        self.refs_hint_label.pack(anchor="w", pady=(0, 4))

        ref_actions = ttk.Frame(self.refs_box)
        ref_actions.pack(fill="x", pady=(2, 4))
        ttk.Button(ref_actions, text="📁 Dosya Seç", command=self.add_ref_files).pack(side="left", padx=(0, 8))
        ttk.Button(ref_actions, text="📂 Klasörden Tara", command=self.scan_refs).pack(side="left", padx=(0, 8))
        ttk.Button(ref_actions, text="❌ Seçileni Çıkar", command=self.remove_selected_refs).pack(side="left", padx=(0, 8))

        self.refs_count_label = ttk.Label(ref_actions, text="", font=("Helvetica", 10, "bold"), foreground="#1565c0")
        self.refs_count_label.pack(side="left", padx=(8, 0))

        self.refs_list = tk.Listbox(self.refs_box, height=4, selectmode=tk.EXTENDED)
        self.refs_list.pack(fill="x", pady=(0, 4))

        self.refs_frame = self.refs_box  # Geriye dönük uyumluluk

        self.drive_box = ttk.LabelFrame(scrollable_frame, text="Google Drive Arşivleme & Teslimat", padding=(10, 8))
        self.drive_box.pack(fill="x", pady=(4, 8))

        self.drive_status_label = ttk.Label(
            self.drive_box,
            text="",
            wraplength=680,
            justify="left",
            font=("Helvetica", 11),
        )
        self.drive_status_label.pack(side="left", fill="x", expand=True)

        ttk.Button(
            self.drive_box,
            text="Ana Klasörü Değiştir",
            command=self._switch_to_settings_drive,
        ).pack(side="right", padx=(8, 0))

        self.drive_var = tk.StringVar()

        # Başlangıçta geçerli proje çerçevesini göster
        if self.current_project == PROJECT_IKIZLER:
            self.ikizler_form_frame.pack(fill="x", before=self.refs_box)
            self.refs_box.configure(text="🧚‍♀️ Karakter & Masal Referans Görselleri (İsteğe Bağlı)")
            self.refs_hint_label.configure(
                text="Masal kahramanları, karakter çizimleri, maskot veya mekan referanslarını ekleyebilirsiniz. "
                     "Gemini 3D Pixar animasyonunda bu karakterlere ve tasarım detaylarına sadık kalır."
            )
        else:
            self.duygu_form_frame.pack(fill="x", before=self.refs_box)
            self.refs_box.configure(text="🎬 Referans Görselleri — Ürün / Kampanya (İsteğe Bağlı)")
            self.refs_hint_label.configure(
                text="Reklam ve tanıtımlarda birebir korunacak ürün, marka veya sahne referans görsellerini ekleyin."
            )

        # -------------------------------------------------------------
        # 4. Ayarlar Sekmesi (Her iki Gem adresi yapılandırılabilir)
        # -------------------------------------------------------------
        self.vars = {}
        fields = [
            ("gem_url_duygu_ai", "Gemini Gem URL (Duygu AI Studio)"),
            ("gem_url_ikizler", "Gemini Gem URL (İkizler Masal Durağı)"),
            ("root_drive_folder", "Ana Google Drive Klasörü (URL veya ID)"),
            ("source_folder", "Kaynak görseller klasörü (isteğe bağlı)"),
            ("chrome_debug_url", "Chrome CDP adresi"),
            ("chrome_app", "Chrome uygulama yolu"),
            ("max_images", "En fazla referans görseli"),
            ("video_timeout_minutes", "Video bekleme süresi (dakika)"),
            ("connect_timeout_ms", "CDP zaman aşımı (ms)"),
            ("retry_limit", "Bağlantı yeniden deneme"),
        ]
        for i, (key, label) in enumerate(fields):
            ttk.Label(self.settings_tab, text=label).grid(row=i, column=0, sticky="w", pady=6)
            var = tk.StringVar()
            self.vars[key] = var
            entry = ttk.Entry(self.settings_tab, textvariable=var, width=76)
            entry.grid(row=i, column=1, sticky="ew", padx=(12, 6), pady=6)
            if key == "root_drive_folder":
                ttk.Button(self.settings_tab, text="Açık Sekmeden Al", command=self.fetch_drive_from_browser).grid(row=i, column=2, pady=6)
            if key == "source_folder":
                ttk.Button(self.settings_tab, text="Klasör seç", command=self.choose_source).grid(row=i, column=2, pady=6)
            if key == "chrome_app":
                ttk.Button(self.settings_tab, text="Uygulama seç", command=self.choose_chrome).grid(row=i, column=2, pady=6)
        self.vars["gem_url"] = tk.StringVar(value=self.cfg.get("gem_url", ""))
        self.settings_tab.columnconfigure(1, weight=1)
        ttk.Button(self.settings_tab, text="Ayarları Kaydet", command=self.save_settings).grid(
            row=len(fields), column=1, sticky="e", pady=12
        )
        ttk.Label(
            self.settings_tab,
            text="Tüm reklam/içerik üretimleri bu ana Google Drive klasörünün altında otomatik olarak kendi klasörünü "
                 "(01_Senaryo, 02_Gorseller, 03_Videolar ve tam_video.mp4) oluşturup oraya yüklenir.\n"
                 "Klasör URL'sini doğrudan yapıştırabilir veya 'Açık Sekmeden Al' ile Chrome'dan çekebilirsiniz.",
            wraplength=760,
        ).grid(row=len(fields) + 1, column=0, columnspan=3, sticky="w", pady=(10, 0))

        self._build_review_tab()

    def _get_pending_job_data(self, project_name: str) -> dict | None:
        """Belirtilen proje için bekleyen veya yarım kalan iş emrini döner."""
        # 1. Projeye özel dosya
        spec_file = get_pending_job_file(project_name)
        if spec_file.is_file():
            try:
                data = json.loads(spec_file.read_text(encoding="utf-8"))
                if isinstance(data, dict) and any(data.get(k) for k in ["brief", "scenario", "masal_topic", "masal_story"]):
                    return data
            except Exception:
                pass

        # 2. Genel pending_job.json dosyası
        if PENDING_JOB_FILE.is_file():
            try:
                data = json.loads(PENDING_JOB_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    # Proje adı eşleşiyorsa
                    if (data.get("project_name") == project_name) and any(data.get(k) for k in ["brief", "scenario", "masal_topic", "masal_story"]):
                        return data
                    # Veya masal alanları dolu ve istenen proje İkizler ise
                    if project_name == PROJECT_IKIZLER and any(data.get(k) for k in ["masal_topic", "masal_story"]):
                        return data
                    # Veya genel brief dolu ve istenen proje Duygu ise
                    if project_name == PROJECT_DUYGU and any(data.get(k) for k in ["brief", "scenario"]):
                        return data
            except Exception:
                pass

        return None

    def prompt_project_selection(self, force=False):
        """Uygulama açılışında kullanıcıya proje seçimini soran şık modal pencere."""
        if not self.winfo_exists():
            return

        dlg = tk.Toplevel(self)
        dlg.title("Proje Seçimi — DuyguAI Automation")
        dlg.geometry("700x460")
        dlg.minsize(660, 420)
        dlg.transient(self)

        try:
            self.update_idletasks()
            x = self.winfo_x() + (self.winfo_width() // 2) - 350
            y = self.winfo_y() + (self.winfo_height() // 2) - 230
            dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
        except Exception:
            pass

        header_frame = ttk.Frame(dlg, padding=(20, 16, 20, 6))
        header_frame.pack(fill="x")

        ttk.Label(
            header_frame,
            text="Hangi proje için içerik üreteceksiniz?",
            font=("Helvetica", 16, "bold"),
            wraplength=660,
        ).pack(anchor="w", pady=(0, 4))

        ttk.Label(
            header_frame,
            text="Lütfen çalışmak istediğiniz projeyi seçin. "
                 "Yarım kalan veya bekleyen bir üretiminiz varsa bilgiler otomatik yüklenecektir:",
            wraplength=660,
            foreground="#555555",
            font=("Helvetica", 11),
        ).pack(anchor="w")

        content_frame = ttk.Frame(dlg, padding=(20, 6, 20, 16))
        content_frame.pack(fill="both", expand=True)
        content_frame.columnconfigure(0, weight=1, uniform="card")
        content_frame.columnconfigure(1, weight=1, uniform="card")
        content_frame.rowconfigure(0, weight=1)

        def select(p_name):
            self.switch_project(p_name, load_pending=True)
            try:
                dlg.destroy()
            except Exception:
                pass

        # 1. Duygu AI Studio Kartı
        duygu_pending = self._get_pending_job_data(PROJECT_DUYGU)
        card1 = ttk.LabelFrame(content_frame, text=" 🎬 Duygu AI Studio ", padding=12)
        card1.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=4)
        ttk.Label(
            card1,
            text="Ticari & Reklam İçerikleri",
            font=("Helvetica", 13, "bold"),
            foreground="#1565c0",
        ).pack(anchor="w", pady=(0, 4))
        ttk.Label(
            card1,
            text="• Ürün ve marka tanıtımları\n"
                 "• Sosyal medya Reels videoları\n"
                 "• Klinik tanıtım videoları\n"
                 "• 3D animasyon ve reklamlar\n\n"
                 "🔗 Gem: af11977bbdce",
            wraplength=270,
        ).pack(anchor="w", fill="both", expand=True)

        if duygu_pending:
            preview_txt = (duygu_pending.get("brief") or "Kayıtlı taslak")[:50]
            ttk.Label(
                card1,
                text=f"📌 Yarım kalan iş: {preview_txt}…",
                foreground="#e65100",
                font=("Helvetica", 10, "italic"),
                wraplength=270,
            ).pack(anchor="w", pady=(4, 6))
            btn1_text = "🎬 Duygu AI Studio (Kaldığın Yerden Devam Et)"
        else:
            btn1_text = "🎬 Duygu AI Studio ile Başla"

        btn1 = ttk.Button(
            card1,
            text=btn1_text,
            command=lambda: select(PROJECT_DUYGU),
        )
        btn1.pack(fill="x", pady=(8, 0))

        # 2. İkizler Masal Durağı Kartı
        ikizler_pending = self._get_pending_job_data(PROJECT_IKIZLER)
        card2 = ttk.LabelFrame(content_frame, text=" 🧚‍♀️ İkizler Masal Durağı ", padding=12)
        card2.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=4)
        ttk.Label(
            card2,
            text="Masal & Çocuk İçerikleri",
            font=("Helvetica", 13, "bold"),
            foreground="#2e7d32",
        ).pack(anchor="w", pady=(0, 4))
        ttk.Label(
            card2,
            text="• Okul öncesi & çocuk masalları\n"
                 "• Karakterler & eğitici hedefler\n"
                 "• Yaş gruplarına özel temalar\n"
                 "• Çocuk animasyon prodüksiyonu\n\n"
                 "🔗 Gem: 8e63671e6329",
            wraplength=270,
        ).pack(anchor="w", fill="both", expand=True)

        if ikizler_pending:
            m_preview = (ikizler_pending.get("masal_topic") or ikizler_pending.get("brief") or "Kayıtlı masal")[:50]
            ttk.Label(
                card2,
                text=f"📌 Yarım kalan masal: {m_preview}…",
                foreground="#2e7d32",
                font=("Helvetica", 10, "italic"),
                wraplength=270,
            ).pack(anchor="w", pady=(4, 6))
            btn2_text = "🧚‍♀️ İkizler Masal Durağı (Kaldığın Yerden Devam Et)"
        else:
            btn2_text = "🧚‍♀️ İkizler Masal Durağı ile Başla"

        btn2 = ttk.Button(
            card2,
            text=btn2_text,
            command=lambda: select(PROJECT_IKIZLER),
        )
        btn2.pack(fill="x", pady=(8, 0))

        try:
            dlg.lift()
            dlg.focus_force()
            dlg.grab_set()
        except Exception:
            pass

    def switch_project(self, project_name: str, load_pending: bool = False):
        """Aktif projeyi değiştirir, ilgili form alanlarını gösterir ve Gem adresini senkronize eder."""
        if project_name not in PROJECTS:
            project_name = PROJECT_DUYGU
        self.current_project = project_name
        self.cfg["selected_project"] = project_name

        if project_name == PROJECT_IKIZLER:
            gem = (
                self.vars.get("gem_url_ikizler", tk.StringVar()).get().strip()
                or self.cfg.get("gem_url_ikizler")
                or DEFAULT_GEM_URLS[PROJECT_IKIZLER]
            )
            self.cfg["gem_url"] = gem
            if "gem_url" in self.vars:
                self.vars["gem_url"].set(gem)
            if hasattr(self, "duygu_form_frame") and hasattr(self, "ikizler_form_frame"):
                target_before = self.refs_box if hasattr(self, "refs_box") else self.drive_box
                self.duygu_form_frame.pack_forget()
                self.ikizler_form_frame.pack(fill="x", before=target_before)
            if hasattr(self, "refs_box"):
                self.refs_box.configure(text="🧚‍♀️ Karakter & Masal Referans Görselleri (İsteğe Bağlı)")
            if hasattr(self, "refs_hint_label"):
                self.refs_hint_label.configure(
                    text="Masal kahramanları, karakter çizimleri, maskot veya mekan referanslarını ekleyebilirsiniz. "
                         "Gemini 3D Pixar animasyonunda bu karakterlere ve tasarım detaylarına sadık kalır."
                )
            if hasattr(self, "project_status_label"):
                self.project_status_label.configure(
                    text=f"Aktif Proje: 🧚‍♀️ {PROJECT_IKIZLER}   (Gem: {gem})"
                )
        else:
            gem = (
                self.vars.get("gem_url_duygu_ai", tk.StringVar()).get().strip()
                or self.cfg.get("gem_url_duygu_ai")
                or DEFAULT_GEM_URLS[PROJECT_DUYGU]
            )
            self.cfg["gem_url"] = gem
            if "gem_url" in self.vars:
                self.vars["gem_url"].set(gem)
            if hasattr(self, "duygu_form_frame") and hasattr(self, "ikizler_form_frame"):
                target_before = self.refs_box if hasattr(self, "refs_box") else self.drive_box
                self.ikizler_form_frame.pack_forget()
                self.duygu_form_frame.pack(fill="x", before=target_before)
            if hasattr(self, "refs_box"):
                self.refs_box.configure(text="🎬 Referans Görselleri — Ürün / Kampanya (İsteğe Bağlı)")
            if hasattr(self, "refs_hint_label"):
                self.refs_hint_label.configure(
                    text="Reklam ve tanıtımlarda birebir korunacak ürün, marka veya sahne referans görsellerini ekleyin."
                )
            if hasattr(self, "project_status_label"):
                self.project_status_label.configure(
                    text=f"Aktif Proje: 🎬 {PROJECT_DUYGU}   (Gem: {gem})"
                )
        save_config(self.cfg)
        self.log(f"📌 Aktif Proje: {project_name} (Gem: {self.cfg['gem_url']})")

        # Projeye ait bekleyen/yarım kalan işi yükle
        if load_pending:
            p_data = self._get_pending_job_data(project_name)
            if p_data:
                self._apply_job_data(p_data)
                self.log(f"📌 {project_name} için yarım kalan üretim bilgileri yüklendi. 'Üretimi Başlat' ile devam edebilirsiniz.")
            else:
                self.reset_project_fields(silent=True)
                self.log(f"✨ {project_name} yeni üretim için hazır.")

    def _load_fields(self):
        # 1. Sistem ayarlarını yükle
        for key, var in self.vars.items():
            if key in SYSTEM_KEYS:
                val = self.cfg.get(key, "")
                if not val and key == "root_drive_folder":
                    val = self.cfg.get("target_folder_id", "")
                var.set(str(val))
        self.dry_run_var.set(_as_bool(self.cfg.get("dry_run"), True))
        self._update_drive_status_label()

        # 2. Aktif projeyi ve bekleyen işi yükle
        self.switch_project(self.current_project, load_pending=True)

    def _apply_job_data(self, data: dict):
        p_name = data.get("project_name") or PROJECT_DUYGU
        if p_name in PROJECTS:
            self.switch_project(p_name)

        # Duygu AI alanları
        self.brief_text.delete("1.0", "end")
        self.brief_text.insert("1.0", data.get("brief", ""))

        self.scenario_text.delete("1.0", "end")
        self.scenario_text.insert("1.0", data.get("scenario", ""))

        self.editing_text.delete("1.0", "end")
        self.editing_text.insert("1.0", data.get("editing_notes", ""))

        extra = data.get("extra_notes") or data.get("prompt") or ""
        self.extra_text.delete("1.0", "end")
        self.extra_text.insert("1.0", extra)

        self.content_type.set(data.get("content_type") or CONTENT_TYPES[0])
        self.video_format.set(data.get("video_format") or VIDEO_FORMATS[0])

        self.scene_count.delete(0, "end")
        self.scene_count.insert(0, str(data.get("scene_count", 1)))

        self.seconds_per_scene.delete(0, "end")
        self.seconds_per_scene.insert(0, str(data.get("seconds_per_scene", 5)))

        self.voiceover_var.set(_as_bool(data.get("voiceover"), False))
        self.music_var.set(_as_bool(data.get("music"), False))

        # İkizler Masal Durağı alanları
        if hasattr(self, "masal_topic_entry"):
            self.masal_topic_entry.delete(0, "end")
            self.masal_topic_entry.insert(0, data.get("masal_topic", ""))
        if hasattr(self, "masal_age_combo"):
            self.masal_age_combo.set(data.get("masal_age") or (self.masal_age_combo["values"][0] if self.masal_age_combo["values"] else ""))
        if hasattr(self, "masal_chars_entry"):
            self.masal_chars_entry.delete(0, "end")
            self.masal_chars_entry.insert(0, data.get("masal_chars", ""))
        if hasattr(self, "masal_goal_entry"):
            self.masal_goal_entry.delete(0, "end")
            self.masal_goal_entry.insert(0, data.get("masal_goal", ""))
        if hasattr(self, "masal_story_text"):
            self.masal_story_text.delete("1.0", "end")
            self.masal_story_text.insert("1.0", data.get("masal_story", data.get("scenario", "")))
        if hasattr(self, "masal_notes_text"):
            self.masal_notes_text.delete("1.0", "end")
            self.masal_notes_text.insert("1.0", data.get("masal_notes", extra))
        if hasattr(self, "ikizler_video_format"):
            fmt = data.get("ikizler_video_format") or data.get("video_format") or VIDEO_FORMATS[0]
            self.ikizler_video_format.set(fmt)
        if hasattr(self, "ikizler_scene_count"):
            cnt = str(data.get("ikizler_scene_count") or data.get("scene_count") or "2")
            self.ikizler_scene_count.delete(0, "end")
            self.ikizler_scene_count.insert(0, cnt)
        if hasattr(self, "ikizler_seconds_per_scene"):
            sec = str(data.get("ikizler_seconds_per_scene") or data.get("seconds_per_scene") or "10")
            self.ikizler_seconds_per_scene.delete(0, "end")
            self.ikizler_seconds_per_scene.insert(0, sec)
        if hasattr(self, "ikizler_voiceover_var"):
            vo = data.get("ikizler_voiceover_var") if "ikizler_voiceover_var" in data else data.get("voiceover", True)
            self.ikizler_voiceover_var.set(_as_bool(vo, True))
        if hasattr(self, "ikizler_music_var"):
            mu = data.get("ikizler_music_var") if "ikizler_music_var" in data else data.get("music", True)
            self.ikizler_music_var.set(_as_bool(mu, True))

        target_id = str(data.get("target_folder_id", ""))
        self.drive_var.set(target_id)
        if "target_folder_id" in self.vars:
            self.vars["target_folder_id"].set(target_id)

        source_f = str(data.get("source_folder", ""))
        if "source_folder" in self.vars:
            self.vars["source_folder"].set(source_f)

        raw_refs = data.get("selected_refs") or []
        self.selected_refs = [Path(p) for p in raw_refs if Path(p).is_file()]
        self._render_refs()

    def reset_project_fields(self, silent=False):
        """Tüm proje/iş emri alanlarını sıfırlar, formu yeni iş için temizler."""
        self.brief_text.delete("1.0", "end")
        self.scenario_text.delete("1.0", "end")
        self.editing_text.delete("1.0", "end")
        self.extra_text.delete("1.0", "end")

        self.content_type.set(CONTENT_TYPES[0])
        self.video_format.set(VIDEO_FORMATS[0])

        self.scene_count.delete(0, "end")
        self.scene_count.insert(0, "1")

        self.seconds_per_scene.delete(0, "end")
        self.seconds_per_scene.insert(0, "5")

        self.voiceover_var.set(False)
        self.music_var.set(False)

        # İkizler Masal Durağı alanlarını sıfırla
        if hasattr(self, "masal_topic_entry"):
            self.masal_topic_entry.delete(0, "end")
        if hasattr(self, "masal_age_combo"):
            vals = self.masal_age_combo["values"]
            self.masal_age_combo.set(vals[0] if vals else "")
        if hasattr(self, "masal_chars_entry"):
            self.masal_chars_entry.delete(0, "end")
        if hasattr(self, "masal_goal_entry"):
            self.masal_goal_entry.delete(0, "end")
        if hasattr(self, "masal_story_text"):
            self.masal_story_text.delete("1.0", "end")
        if hasattr(self, "masal_notes_text"):
            self.masal_notes_text.delete("1.0", "end")
        if hasattr(self, "ikizler_video_format"):
            self.ikizler_video_format.set(VIDEO_FORMATS[0])
        if hasattr(self, "ikizler_scene_count"):
            self.ikizler_scene_count.delete(0, "end")
            self.ikizler_scene_count.insert(0, "3")
        if hasattr(self, "ikizler_seconds_per_scene"):
            self.ikizler_seconds_per_scene.delete(0, "end")
            self.ikizler_seconds_per_scene.insert(0, "5")
        if hasattr(self, "ikizler_voiceover_var"):
            self.ikizler_voiceover_var.set(True)
        if hasattr(self, "ikizler_music_var"):
            self.ikizler_music_var.set(True)

        self.drive_var.set("")
        self._update_drive_status_label()

        self.selected_refs = []
        self._render_refs()

        if not silent:
            self.log("🔄 Form ve proje bilgileri sıfırlandı. Yeni üretim için hazır.")

    def confirm_reset_form(self):
        has_content = bool(
            self.brief_text.get("1.0", "end").strip()
            or self.scenario_text.get("1.0", "end").strip()
            or (hasattr(self, "masal_topic_entry") and self.masal_topic_entry.get().strip())
            or (hasattr(self, "masal_story_text") and self.masal_story_text.get("1.0", "end").strip())
            or self.selected_refs
            or self.drive_var.get().strip()
        )
        if has_content:
            if not messagebox.askyesno(
                "Formu Sıfırla",
                "Mevcut form bilgileri ve referanslar sıfırlanacak. Yeni bir iş emri başlatmak istiyor musunuz?",
            ):
                return
        self._clear_pending_job()
        self.reset_project_fields(silent=False)

    def _save_pending_job(self):
        draft = {
            "project_name": self.current_project,
            "brief": self.brief_text.get("1.0", "end").strip(),
            "scenario": self.scenario_text.get("1.0", "end").strip(),
            "editing_notes": self.editing_text.get("1.0", "end").strip(),
            "extra_notes": self.extra_text.get("1.0", "end").strip(),
            "content_type": self.content_type.get().strip(),
            "video_format": self.video_format.get().strip(),
            "scene_count": self.scene_count.get(),
            "seconds_per_scene": self.seconds_per_scene.get(),
            "voiceover": bool(self.voiceover_var.get()),
            "music": bool(self.music_var.get()),
            "masal_topic": self.masal_topic_entry.get().strip() if hasattr(self, "masal_topic_entry") else "",
            "masal_age": self.masal_age_combo.get().strip() if hasattr(self, "masal_age_combo") else "",
            "masal_chars": self.masal_chars_entry.get().strip() if hasattr(self, "masal_chars_entry") else "",
            "masal_goal": self.masal_goal_entry.get().strip() if hasattr(self, "masal_goal_entry") else "",
            "masal_story": self.masal_story_text.get("1.0", "end").strip() if hasattr(self, "masal_story_text") else "",
            "masal_notes": self.masal_notes_text.get("1.0", "end").strip() if hasattr(self, "masal_notes_text") else "",
            "ikizler_video_format": self.ikizler_video_format.get().strip() if hasattr(self, "ikizler_video_format") else "",
            "ikizler_scene_count": self.ikizler_scene_count.get() if hasattr(self, "ikizler_scene_count") else "",
            "ikizler_seconds_per_scene": self.ikizler_seconds_per_scene.get() if hasattr(self, "ikizler_seconds_per_scene") else "",
            "ikizler_voiceover_var": bool(self.ikizler_voiceover_var.get()) if hasattr(self, "ikizler_voiceover_var") else True,
            "ikizler_music_var": bool(self.ikizler_music_var.get()) if hasattr(self, "ikizler_music_var") else True,
            "target_folder_id": self.drive_var.get().strip(),
            "source_folder": self.vars.get("source_folder", tk.StringVar()).get().strip(),
            "selected_refs": [str(p) for p in self.selected_refs],
        }
        try:
            JOBS_DIR.mkdir(parents=True, exist_ok=True)
            # 1. Projeye özel dosya
            spec_file = get_pending_job_file(self.current_project)
            spec_file.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
            # 2. Genel pending dosya
            PENDING_JOB_FILE.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            self.log(f"Bekleyen iş emri kaydedilemedi: {e}")

    def _clear_pending_job(self):
        spec_file = get_pending_job_file(self.current_project)
        for f in [spec_file, PENDING_JOB_FILE]:
            if f.is_file():
                try:
                    f.unlink()
                except Exception:
                    pass

    def _clean_config_project_keys(self):
        clean_cfg = {}
        for k in SYSTEM_KEYS:
            if k in self.cfg:
                clean_cfg[k] = self.cfg[k]
        self.cfg = clean_cfg
        save_config(self.cfg)

    def log(self, message):
        self.log_queue.put(str(message))

    def _drain_log(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", line + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(150, self._drain_log)

    def save_settings(self, silent=False):
        for key, var in self.vars.items():
            if key in SYSTEM_KEYS:
                value = var.get().strip()
                if key == "root_drive_folder":
                    value = extract_drive_folder_id(value)
                    var.set(value)
                elif key in ("max_images", "video_timeout_minutes", "connect_timeout_ms", "retry_limit"):
                    try:
                        value = int(value)
                    except ValueError:
                        defaults = {
                            "max_images": 4,
                            "video_timeout_minutes": 20,
                            "connect_timeout_ms": 20000,
                            "retry_limit": 2,
                        }
                        value = defaults.get(key, 0)
                self.cfg[key] = value
        if "root_drive_folder" in self.cfg:
            self.cfg["target_folder_id"] = self.cfg["root_drive_folder"]
        self.cfg["dry_run"] = bool(self.dry_run_var.get())
        if self.current_project == PROJECT_IKIZLER:
            self.cfg["gem_url"] = self.cfg.get("gem_url_ikizler", DEFAULT_GEM_URLS[PROJECT_IKIZLER])
        else:
            self.cfg["gem_url"] = self.cfg.get("gem_url_duygu_ai", DEFAULT_GEM_URLS[PROJECT_DUYGU])
        self.cfg["selected_project"] = self.current_project
        save_config(self.cfg)
        self._update_drive_status_label()
        if not silent:
            messagebox.showinfo("Ayarlar", "Sistem ayarları kaydedildi.")

    def _switch_to_settings_drive(self):
        self.tabs.select(self.settings_tab)

    def _update_drive_status_label(self):
        root_id = (
            self.vars.get("root_drive_folder", tk.StringVar()).get().strip()
            or self.cfg.get("root_drive_folder")
            or self.cfg.get("target_folder_id")
            or ""
        ).strip()
        if root_id:
            root_id = extract_drive_folder_id(root_id)
            self.drive_status_label.configure(
                text=f"📁 Ana Drive Klasörü: {root_id}\n"
                     f"   ↳ Her yeni reklamda hem yerelde hem Drive'da otomatik alt klasörler "
                     f"(01_Senaryo, 02_Gorseller, 03_Videolar, tam_video.mp4) oluşturulacaktır.",
                foreground="#1b5e20",
            )
        else:
            self.drive_status_label.configure(
                text="⚠️ Ana Google Drive klasörü henüz tanımlanmadı.\n"
                     "   ↳ 'Ana Klasörü Değiştir' butonuna tıklayarak Drive linkinizi veya klasör ID'nizi belirleyebilirsiniz.",
                foreground="#b71c1c",
            )

    def fetch_drive_from_browser(self):
        """Açık Chrome sekmesindeki Google Drive klasör URL'sini veya ID'sini otomatik algılar."""
        cfg = self._get_current_job_cfg()
        cdp_url = cfg.get("chrome_debug_url") or "http://127.0.0.1:9222"
        self.log("🔍 Chrome CDP üzerinden açık Google Drive klasör sekmeleri taranıyor…")
        def work():
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    browser = p.chromium.connect_over_cdp(cdp_url, timeout=6000)
                    pages = [pg.url for context in browser.contexts for pg in context.pages]
                    browser.close()
                    drive_urls = [u for u in pages if "drive.google.com/drive" in u and "folders" in u]
                    if not drive_urls:
                        self.log("Chrome'da açık bir Google Drive klasör sekmesi bulunamadı.")
                        self.after(0, lambda: messagebox.showinfo(
                            "Drive Algılama",
                            "Chrome'da açık bir Google Drive klasör sekmesi bulunamadı.\n\n"
                            "Lütfen Chrome'da ana klasörünüzü açın veya URL'sini manuel yapıştırın."
                        ))
                        return
                    found_id = extract_drive_folder_id(drive_urls[0])
                    if found_id:
                        self.after(0, lambda: self._apply_fetched_drive_id(found_id))
            except Exception as e:
                self.log(f"Drive klasörü algılama hatası: {e}")
                self.after(0, lambda: messagebox.showerror(
                    "Bağlantı Hatası",
                    f"Chrome'a bağlanılamadı:\n{e}\n\nLütfen önce 'Chrome'u Başlat' butonunu kullanın."
                ))
        threading.Thread(target=work, daemon=True).start()

    def _apply_fetched_drive_id(self, found_id: str):
        if "root_drive_folder" in self.vars:
            self.vars["root_drive_folder"].set(found_id)
        if "target_folder_id" in self.vars:
            self.vars["target_folder_id"].set(found_id)
        self.cfg["root_drive_folder"] = found_id
        self.cfg["target_folder_id"] = found_id
        save_config(self.cfg)
        self._update_drive_status_label()
        self.log(f"✅ Açık Drive sekmesinden ana klasör ID'si alındı ve kaydedildi: {found_id}")
        messagebox.showinfo("Başarılı", f"Ana Google Drive klasörü ayarlandı:\n{found_id}")

    def choose_source(self):
        p = filedialog.askdirectory(title="Kaynak görseller klasörünü seç")
        if p:
            self.vars["source_folder"].set(p)

    def choose_chrome(self):
        p = filedialog.askdirectory(title="Google Chrome.app klasörünü seç")
        if p:
            self.vars["chrome_app"].set(p)

    def _get_current_job_cfg(self):
        job_cfg = dict(self.cfg)
        for key, var in self.vars.items():
            value = var.get().strip()
            if key in ("max_images", "video_timeout_minutes", "connect_timeout_ms", "retry_limit"):
                try:
                    value = int(value)
                except ValueError:
                    value = 0
            job_cfg[key] = value

        job_cfg["project_name"] = self.current_project
        if self.current_project == PROJECT_IKIZLER:
            topic = self.masal_topic_entry.get().strip() if hasattr(self, "masal_topic_entry") else ""
            age = self.masal_age_combo.get().strip() if hasattr(self, "masal_age_combo") else ""
            chars = self.masal_chars_entry.get().strip() if hasattr(self, "masal_chars_entry") else ""
            goal = self.masal_goal_entry.get().strip() if hasattr(self, "masal_goal_entry") else ""
            story = self.masal_story_text.get("1.0", "end").strip() if hasattr(self, "masal_story_text") else ""
            notes = self.masal_notes_text.get("1.0", "end").strip() if hasattr(self, "masal_notes_text") else ""

            brief_parts = []
            if topic:
                brief_parts.append(f"Masal Konusu: {topic}")
            if age:
                brief_parts.append(f"Hedef Yaş Grubu: {age}")
            if chars:
                brief_parts.append(f"Karakterler: {chars}")
            if goal:
                brief_parts.append(f"Eğitsel Hedef: {goal}")
            masal_brief = "\n".join(brief_parts)

            job_cfg["brief"] = masal_brief
            job_cfg["scenario"] = story
            job_cfg["editing_notes"] = ""
            job_cfg["extra_notes"] = notes
            job_cfg["prompt"] = notes
            job_cfg["content_type"] = "çocuk masalı"
            job_cfg["video_format"] = (self.ikizler_video_format.get().strip() if hasattr(self, "ikizler_video_format") else "") or VIDEO_FORMATS[0]
            try:
                job_cfg["scene_count"] = int(self.ikizler_scene_count.get())
            except (ValueError, AttributeError):
                job_cfg["scene_count"] = 3
            try:
                job_cfg["seconds_per_scene"] = int(self.ikizler_seconds_per_scene.get())
            except (ValueError, AttributeError):
                job_cfg["seconds_per_scene"] = 5
            job_cfg["voiceover"] = bool(self.ikizler_voiceover_var.get()) if hasattr(self, "ikizler_voiceover_var") else True
            job_cfg["music"] = bool(self.ikizler_music_var.get()) if hasattr(self, "ikizler_music_var") else True
            gem = (
                self.vars.get("gem_url_ikizler", tk.StringVar()).get().strip()
                or self.cfg.get("gem_url_ikizler")
                or DEFAULT_GEM_URLS[PROJECT_IKIZLER]
            )
            job_cfg["gem_url"] = gem
            job_cfg["masal_topic"] = topic
            job_cfg["masal_age"] = age
            job_cfg["masal_chars"] = chars
            job_cfg["masal_goal"] = goal
            job_cfg["masal_story"] = story
            job_cfg["masal_notes"] = notes
        else:
            job_cfg["brief"] = self.brief_text.get("1.0", "end").strip()
            job_cfg["scenario"] = self.scenario_text.get("1.0", "end").strip()
            job_cfg["editing_notes"] = self.editing_text.get("1.0", "end").strip()
            extra = self.extra_text.get("1.0", "end").strip()
            job_cfg["extra_notes"] = extra
            job_cfg["prompt"] = extra
            job_cfg["content_type"] = self.content_type.get().strip() or CONTENT_TYPES[0]
            job_cfg["video_format"] = self.video_format.get().strip() or VIDEO_FORMATS[0]
            try:
                job_cfg["scene_count"] = int(self.scene_count.get())
            except ValueError:
                job_cfg["scene_count"] = 1
            try:
                job_cfg["seconds_per_scene"] = int(self.seconds_per_scene.get())
            except ValueError:
                job_cfg["seconds_per_scene"] = 5
            job_cfg["voiceover"] = bool(self.voiceover_var.get())
            job_cfg["music"] = bool(self.music_var.get())
            gem = (
                self.vars.get("gem_url_duygu_ai", tk.StringVar()).get().strip()
                or self.cfg.get("gem_url_duygu_ai")
                or DEFAULT_GEM_URLS[PROJECT_DUYGU]
            )
            job_cfg["gem_url"] = gem

        effective_drive = (
            self.drive_var.get().strip()
            or self.vars.get("root_drive_folder", tk.StringVar()).get().strip()
            or self.cfg.get("root_drive_folder", "")
            or self.cfg.get("target_folder_id", "")
        )
        effective_drive_id = extract_drive_folder_id(effective_drive)
        job_cfg["root_drive_folder"] = effective_drive_id
        job_cfg["target_folder_id"] = effective_drive_id
        return job_cfg

    def _refresh_cfg(self):
        return self._get_current_job_cfg()

    def _render_refs(self):
        if hasattr(self, "refs_list"):
            self.refs_list.delete(0, "end")
            for p in self.selected_refs:
                self.refs_list.insert("end", str(p))
        cnt = len(self.selected_refs)
        badge = f"({cnt} görsel seçili)" if cnt > 0 else ""
        if hasattr(self, "refs_count_label"):
            self.refs_count_label.configure(text=badge)
        if hasattr(self, "ikizler_ref_status_lbl"):
            self.ikizler_ref_status_lbl.configure(
                text=f"✅ {cnt} karakter/masal görseli seçildi" if cnt > 0 else ""
            )

    def scan_refs(self):
        folder = self.vars.get("source_folder", tk.StringVar()).get().strip() or self.cfg.get("source_folder", "")
        if not folder or not Path(folder).is_dir():
            p = filedialog.askdirectory(title="Referans görseller klasörünü seç")
            if p:
                folder = p
                if "source_folder" in self.vars:
                    self.vars["source_folder"].set(p)
            else:
                return
        limit = max(1, int(self.cfg.get("max_images", 4)))
        found = scan_reference_images(folder, limit=limit)
        merged = unique_existing_images([*found, *self.selected_refs], limit=limit)
        self.selected_refs = merged
        self._render_refs()
        self.log(f"{len(self.selected_refs)} görsel listede (klasör taraması: {Path(folder).name}).")

    def add_ref_files(self):
        cfg = self._get_current_job_cfg()
        paths = filedialog.askopenfilenames(
            title="Referans görselleri seç",
            filetypes=[("Görseller", "*.png *.jpg *.jpeg *.webp *.bmp"), ("Tümü", "*.*")],
        )
        if not paths:
            return
        limit = max(1, int(cfg.get("max_images", 4)))
        merged = unique_existing_images([*self.selected_refs, *paths], limit=limit)
        self.selected_refs = merged
        self._render_refs()
        self.log(f"{len(self.selected_refs)} referans görseli seçili.")

    def remove_selected_refs(self):
        selected = set(self.refs_list.curselection())
        if not selected:
            return
        self.selected_refs = [p for i, p in enumerate(self.selected_refs) if i not in selected]
        self._render_refs()

    def open_outputs(self):
        out_dir = APP_DIR / "outputs"
        out_dir.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["open", str(out_dir)])

    def launch_chrome(self):
        cfg = self._get_current_job_cfg()
        app = cfg.get("chrome_app") or "/Applications/Google Chrome.app"
        binary = os.path.join(app, "Contents", "MacOS", "Google Chrome")
        if not os.path.exists(binary):
            messagebox.showerror("Chrome", f"Chrome çalıştırılabilir dosyası bulunamadı:\n{binary}")
            return
        profile = os.path.expanduser("~/Chrome-Automation-Profile")
        gem_url = cfg.get("gem_url", "").strip()
        cmd = [
            binary,
            "--remote-debugging-port=9222",
            "--remote-allow-origins=*",
            f"--user-data-dir={profile}",
            "--no-first-run",
        ]
        if gem_url:
            cmd.append(gem_url)
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.log(f"Chrome başlatma isteği gönderildi ({self.current_project}). Gem sekmesini kontrol edin: {gem_url}")
        except Exception as e:
            messagebox.showerror("Chrome", str(e))

    def test_connection(self):
        cfg = self._get_current_job_cfg()
        def work():
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    browser = p.chromium.connect_over_cdp(cfg["chrome_debug_url"], timeout=12000)
                    pages = [pg.url for context in browser.contexts for pg in context.pages]
                    self.log("CDP bağlantısı başarılı.")
                    for url in pages[:12]:
                        self.log("Sekme: " + url)
                    browser.close()
            except Exception as e:
                self.log("CDP bağlantı hatası: " + str(e))
        threading.Thread(target=work, daemon=True).start()

    def start_run(self):
        if self._running:
            messagebox.showwarning("Üretim", "Bir üretim zaten çalışıyor. Paralel başlatma yok.")
            return

        cfg = self._get_current_job_cfg()

        brief = cfg.get("brief") or ""
        scenario = cfg.get("scenario") or ""
        extra = cfg.get("extra_notes") or cfg.get("prompt") or ""
        refs = list(self.selected_refs)

        validation = validate_job_inputs(
            brief=brief,
            scenario=scenario,
            extra_notes=extra,
            content_type=cfg.get("content_type") or "ürün reklamı",
            video_format=cfg.get("video_format") or "9:16",
            scene_count=cfg.get("scene_count") or 1,
            seconds_per_scene=cfg.get("seconds_per_scene") or 5,
            gem_url=cfg.get("gem_url") or "",
            target_folder_id=cfg.get("target_folder_id") or "",
            root_drive_folder=cfg.get("root_drive_folder") or "",
            dry_run=_as_bool(cfg.get("dry_run"), True),
            reference_images=refs,
            project_name=cfg.get("project_name", PROJECT_DUYGU),
        )
        if not validation.ok:
            messagebox.showerror("Eksik veya geçersiz girdi", "\n".join(validation.errors))
            return

        if not brief.strip() and not scenario.strip() and not extra.strip():
            pass
        elif not brief.strip():
            msg = (
                "Masal konusu ve tema boş. Yalnızca sahne taslağı veya ek notlarla devam edilsin mi?"
                if cfg.get("project_name") == PROJECT_IKIZLER
                else "İçerik açıklaması boş. Yalnızca hazır senaryo veya ek notlarla devam edilsin mi?"
            )
            if not messagebox.askyesno("Brief boş", msg):
                return

        dry_run = _as_bool(cfg.get("dry_run"), True)
        if not dry_run:
            if not messagebox.askyesno(
                "Gerçek üretim onayı",
                "Güvenli test KAPALI.\n\n"
                "Bu işlem Gemini'ye gerçek üretim isteği gönderebilir ve maliyet oluşturabilir. "
                "Videolar üretildikten sonra birleştirilecek ve Google Drive'a yüklenecek.\n\n"
                "Devam etmek istiyor musunuz?",
                icon="warning",
            ):
                return

        # Süreç başlamadan önce aktif iş emrini kaydet (hata/kesinti durumunda korunabilmesi için)
        self._save_pending_job()
        try:
            job_order = create_job_order(cfg, refs)
            job_id = self.job_queue.enqueue_job(job_order)
            cfg["job_id"] = job_id
            self.log(f"📋 İş kalıcı kuyruğa kaydedildi (ID: {job_id})")
        except Exception as q_err:
            self.log(f"Kuyruk kayıt uyarısı: {q_err}")

        self._running = True
        self.run_btn.configure(state="disabled")
        if self.stop_btn:
            self.stop_btn.configure(state="normal")
        if self.review_stop_btn:
            self.review_stop_btn.configure(state="normal")
        logger = SafeLogger(self.log, LOGS_DIR / "automation.log")
        now_str = datetime.now().strftime("%H:%M:%S")
        self.log(f"\n{'='*55}\n🚀 [YENİ ÜRETİM BAŞLATILDI] ({now_str})\n{'='*55}")

        def work():
            report = None
            try:
                report = Automation(cfg, logger).run(refs)
            except Exception as e:
                logger("HATA: " + str(e))
            finally:
                try:
                    self.after(0, lambda: self._on_run_finished(report))
                except Exception:
                    pass

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _on_run_finished(self, report):
        self._running = False
        self.run_btn.configure(state="normal")
        if self.stop_btn:
            self.stop_btn.configure(state="disabled")
        if self.review_regen_btn:
            self.review_regen_btn.configure(state="normal")
        if self.review_stop_btn:
            self.review_stop_btn.configure(state="disabled")
        self.refresh_review_tab()
        self.refresh_queue_tab()
        if report is not None and report.overall_status == "success" and report.delivered:
            # Süreç başarıyla tamamlandı: Ekran ve arka plandaki proje verilerini sıfırla
            self._clear_pending_job()
            self._clean_config_project_keys()
            self.reset_project_fields(silent=True)
            self.log("🎉 [BAŞARILI] Üretim ve Drive teslimatı başarıyla tamamlandı!")
            self.log("🔄 Ekran ve arka plandaki proje bilgileri sıfırlandı. Yeni üretim için hazırsınız.")
            try:
                messagebox.showinfo(
                    "Başarılı",
                    "İçerik üretimi ve Google Drive teslimatı başarıyla tamamlandı!\n\nForm yeni üretim için temizlendi."
                )
            except Exception:
                pass
        elif report is not None and report.overall_status == "dry_run":
            self.log("ℹ️ [GÜVENLİ TEST] Simülasyon tamamlandı. Gerçek üretim için 'Güvenli test' onayını kaldırıp başlatabilirsiniz.")
        else:
            # Süreç hata, rate limit veya kota nedeniyle yarım kaldı: BİLGİLERİ KORU
            self._save_pending_job()
            self.log("⚠️ [BEKLEMEDE] Üretim süreci tamamlanamadı veya yarım kaldı.")
            self.log("📌 Form bilgileri, senaryo ve referanslar korundu; 'Üretimi Başlat' ile kaldığınız yerden hemen devam edebilirsiniz.")

    def stop_production(self):
        """Kullanıcı durdurma isteği: Aktif Gemini tarayıcı oturumlarını, kuyruk motorunu ve iş parçacıklarını anında sonlandırır."""
        self._save_pending_job()
        self.log("🛑 Motor / üretim durdurma isteği alındı. İşlemler ve tarayıcı oturumu sonlandırılıyor…")
        try:
            from providers.gemini import GeminiWebProvider
            GeminiWebProvider.abort_all()
        except Exception as e:
            self.log(f"Gemini oturumlarını sonlandırma notu: {e}")

        try:
            if hasattr(self, "queue_runner") and self.queue_runner:
                self.queue_runner.stop()
        except Exception as e:
            self.log(f"Kuyruk motoru durdurma notu: {e}")

        self._running = False
        if hasattr(self, "run_btn") and self.run_btn:
            self.run_btn.configure(state="normal")
        if hasattr(self, "stop_btn") and self.stop_btn:
            self.stop_btn.configure(state="disabled")
        if hasattr(self, "review_regen_btn") and self.review_regen_btn:
            self.review_regen_btn.configure(state="normal")
        if hasattr(self, "review_stop_btn") and self.review_stop_btn:
            self.review_stop_btn.configure(state="disabled")

        self.refresh_queue_tab()
        self.refresh_review_tab()
        self.log("⚪ Üretim durduruldu. Bilgileriniz korundu; 'Üretimi Başlat' ile devam edebilir veya 'Formu Sıfırla' ile sıfırlayabilirsiniz.")

    # -------------------------------------------------------------------------
    # Sahne Kontrolü & Tekrar Üretim (Review Tab & Selective Regeneration)
    # -------------------------------------------------------------------------
    def _build_review_tab(self):
        # 1. Üst Seçim & Kontrol Çubuğu
        top_bar = ttk.Frame(self.review_tab, padding=(4, 4, 4, 6))
        top_bar.pack(side="top", fill="x")

        ttk.Label(top_bar, text="📁 Proje Klasörü:", font=("", 10, "bold")).pack(side="left", padx=(0, 6))
        self.review_folder_combo = ttk.Combobox(top_bar, textvariable=self.review_folder_var, state="readonly", width=36)
        self.review_folder_combo.pack(side="left", padx=(0, 6))
        self.review_folder_combo.bind("<<ComboboxSelected>>", self._load_selected_review_folder)

        ttk.Button(top_bar, text="🔄 Yenile", command=self.refresh_review_tab).pack(side="left", padx=(0, 6))
        ttk.Button(top_bar, text="📂 Klasörü Finder'da Aç", command=self._open_current_review_folder).pack(side="left", padx=(0, 6))

        # 2. Durum / Özet Bilgi Çubuğu
        self.review_info_lbl = ttk.Label(self.review_tab, text="", font=("", 10, "bold"), foreground="#1d4ed8")
        self.review_info_lbl.pack(side="top", fill="x", padx=6, pady=(0, 6))

        # 3. Hızlı Seçim ve Eylem Çubuğu
        actions_bar = ttk.Frame(self.review_tab, padding=(4, 4, 4, 6))
        actions_bar.pack(side="top", fill="x")

        ttk.Button(actions_bar, text="Tüm Görselleri Seç", command=lambda: self.select_all_review_items("images")).pack(side="left", padx=(0, 4))
        ttk.Button(actions_bar, text="Tüm Videoları Seç", command=lambda: self.select_all_review_items("videos")).pack(side="left", padx=(0, 4))
        ttk.Button(actions_bar, text="⚡ Eksikleri Seç", command=self.select_missing_review_items).pack(side="left", padx=(0, 4))
        ttk.Button(actions_bar, text="Tümünü Seç", command=lambda: self.select_all_review_items("both")).pack(side="left", padx=(0, 4))
        ttk.Button(actions_bar, text="Seçimleri Kaldır", command=self.clear_all_review_items).pack(side="left", padx=(0, 10))

        self.review_regen_btn = ttk.Button(
            actions_bar,
            text="🚀 Seçilenleri Üret / Tekrar Üret",
            command=self.regenerate_selected_scenes,
        )
        self.review_regen_btn.pack(side="left", padx=(0, 6))

        self.review_merge_btn = ttk.Button(
            actions_bar,
            text="🎬 Sadece Videoları Birleştir (Hızlı)",
            command=self.quick_merge_review_videos,
        )
        self.review_merge_btn.pack(side="left", padx=(0, 6))

        self.review_stop_btn = ttk.Button(
            actions_bar,
            text="🛑 Üretimi Durdur",
            command=self.stop_production,
            state="disabled",
        )
        self.review_stop_btn.pack(side="left")

        # 4. Kaydırılabilir Sahne Listesi (Canvas + Scrollbar)
        scroll_container = ttk.Frame(self.review_tab)
        scroll_container.pack(side="top", fill="both", expand=True)

        self.review_canvas = tk.Canvas(scroll_container, borderwidth=0, highlightthickness=0)
        v_scroll = ttk.Scrollbar(scroll_container, orient="vertical", command=self.review_canvas.yview)
        self.review_canvas.configure(yscrollcommand=v_scroll.set)

        v_scroll.pack(side="right", fill="y")
        self.review_canvas.pack(side="left", fill="both", expand=True)

        self.review_cards_frame = ttk.Frame(self.review_canvas)
        canvas_win = self.review_canvas.create_window((0, 0), window=self.review_cards_frame, anchor="nw")

        def _on_cards_frame_cfg(event):
            self.review_canvas.configure(scrollregion=self.review_canvas.bbox("all"))

        def _on_review_canvas_cfg(event):
            self.review_canvas.itemconfig(canvas_win, width=event.width)

        self.review_cards_frame.bind("<Configure>", _on_cards_frame_cfg)
        self.review_canvas.bind("<Configure>", _on_review_canvas_cfg)

        def _on_rc_wheel(event):
            if event.delta:
                self.review_canvas.yview_scroll(int(-1 * event.delta), "units")

        self.review_canvas.bind("<Enter>", lambda e: self.review_canvas.bind_all("<MouseWheel>", _on_rc_wheel))
        self.review_canvas.bind("<Leave>", lambda e: self.review_canvas.unbind_all("<MouseWheel>"))

    def _on_notebook_tab_changed(self, event=None):
        try:
            selected_tab = self.tabs.nametowidget(self.tabs.select())
            if selected_tab == self.review_tab:
                self.refresh_review_tab()
            elif selected_tab == self.queue_tab:
                self.refresh_queue_tab()
        except Exception:
            pass

    def switch_to_review_tab(self):
        self.tabs.select(self.review_tab)
        self.refresh_review_tab()

    def refresh_review_tab(self):
        if not OUTPUTS_DIR.exists():
            OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

        project_dirs = []
        for p in OUTPUTS_DIR.iterdir():
            if p.is_dir() and not p.name.startswith("."):
                has_sub = any((p / sub).exists() for sub in ("01_Senaryo", "02_Gorseller", "03_Videolar"))
                if has_sub:
                    project_dirs.append(p)
                else:
                    for sub_p in p.iterdir():
                        if sub_p.is_dir() and not sub_p.name.startswith("."):
                            if any((sub_p / sub).exists() for sub in ("01_Senaryo", "02_Gorseller", "03_Videolar")):
                                project_dirs.append(sub_p)

        project_dirs.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        dir_names = [p.name for p in project_dirs]

        if not self.review_folder_combo:
            return

        self.review_folder_combo["values"] = dir_names
        current = self.review_folder_var.get()
        if dir_names:
            if current not in dir_names:
                self.review_folder_var.set(dir_names[0])
            self._load_selected_review_folder()
        else:
            self.review_folder_var.set("")
            if self.review_info_lbl:
                self.review_info_lbl.configure(text="Henüz tamamlanan veya kaydedilen çıktı klasörü bulunamadı.")
            for widget in self.review_cards_frame.winfo_children():
                widget.destroy()

    def _load_selected_review_folder(self, event=None):
        name = self.review_folder_var.get().strip()
        if not name:
            return

        target_dir = OUTPUTS_DIR / name
        if not target_dir.exists():
            for sub in OUTPUTS_DIR.rglob(name):
                if sub.is_dir():
                    target_dir = sub
                    break

        scenarios_dir = target_dir / "01_Senaryo"
        images_dir = target_dir / "02_Gorseller"
        videos_dir = target_dir / "03_Videolar"
        plan_file = scenarios_dir / "sahne_plani.json"

        scenes = []
        if plan_file.exists():
            try:
                scenes = json.loads(plan_file.read_text(encoding="utf-8"))
            except Exception as e:
                self.log(f"Sahne planı okunamadı ({name}): {e}")

        if not scenes:
            found_indices = set()
            if images_dir.exists():
                for f in images_dir.glob("sahne_*.png"):
                    m = re.search(r"sahne_(\d+)\.png", f.name)
                    if m:
                        found_indices.add(int(m.group(1)))
            if videos_dir.exists():
                for f in videos_dir.glob("sahne_*.mp4"):
                    m = re.search(r"sahne_(\d+)\.mp4", f.name)
                    if m:
                        found_indices.add(int(m.group(1)))
            for idx in sorted(found_indices):
                scenes.append({
                    "index": idx,
                    "title": f"Sahne {idx}",
                    "description": "",
                    "duration_seconds": 5,
                    "voiceover_text": "",
                    "image_prompt": "",
                    "video_prompt": "",
                })

        self.review_current_scenes = scenes

        ready_imgs = 0
        ready_vids = 0
        for sc in scenes:
            idx = sc.get("index", 1)
            if (images_dir / f"sahne_{idx}.png").exists() and (images_dir / f"sahne_{idx}.png").stat().st_size > 20000:
                ready_imgs += 1
            if (videos_dir / f"sahne_{idx}.mp4").exists() and (videos_dir / f"sahne_{idx}.mp4").stat().st_size > 100000:
                ready_vids += 1

        tam_video = videos_dir / "tam_video.mp4"
        tam_video_status = "❌ Tam Video Yok"
        if tam_video.exists() and tam_video.stat().st_size > 1000:
            mb = tam_video.stat().st_size / (1024 * 1024)
            tam_video_status = f"🎬 Tam Video Hazır ({mb:.1f} MB)"

        delivered_badge = " | ☁️ Drive Teslim Edildi" if (target_dir / ".delivered").exists() else ""
        info_txt = (
            f"📁 {name}  |  {len(scenes)} Sahne  |  "
            f"🖼️ Görseller: {ready_imgs}/{len(scenes)}  |  "
            f"🎥 Videolar: {ready_vids}/{len(scenes)}  |  "
            f"{tam_video_status}{delivered_badge}"
        )
        if self.review_info_lbl:
            self.review_info_lbl.configure(text=info_txt)

        self._render_review_scenes(target_dir)

    def _render_review_scenes(self, target_dir: Path):
        for widget in self.review_cards_frame.winfo_children():
            widget.destroy()

        self.regen_img_vars.clear()
        self.regen_vid_vars.clear()
        self.review_img_text_widgets.clear()
        self.review_vid_text_widgets.clear()

        images_dir = target_dir / "02_Gorseller"
        videos_dir = target_dir / "03_Videolar"
        scenarios_dir = target_dir / "01_Senaryo"

        # --- INSTAGRAM PAYLAŞIM VE KAPAK KARTI ---
        insta_file = (scenarios_dir / "instagram_paylasim.txt") if scenarios_dir.exists() else None
        if not (insta_file and insta_file.exists()):
            insta_file = target_dir / "instagram_paylasim.txt"

        cover_file = None
        for cand_cover in [images_dir / "kapak.png", videos_dir / "kapak.png", target_dir / "kapak.png"]:
            if cand_cover.exists() and cand_cover.stat().st_size > 500:
                cover_file = cand_cover
                break

        tam_video_file = videos_dir / "tam_video.mp4"
        insta_text = ""
        if insta_file and insta_file.exists():
            try:
                insta_text = insta_file.read_text(encoding="utf-8").strip()
            except Exception:
                pass

        if insta_text or cover_file or tam_video_file.exists():
            ig_card = ttk.LabelFrame(
                self.review_cards_frame,
                text="  📱 Instagram Paylaşım Paketi & Kapak Fotoğrafı  ",
                padding=8,
            )
            ig_card.pack(fill="x", padx=4, pady=(4, 10))

            ig_header = ttk.Frame(ig_card)
            ig_header.pack(fill="x", pady=(0, 6))

            status_txt = "✅ Paylaşım Metni Hazır" if insta_text else "⏳ Paylaşım Metni Bekleniyor"
            if cover_file:
                status_txt += f" | 🖼️ Kapak Fotoğrafı: {cover_file.name}"
            ttk.Label(ig_header, text=status_txt, font=("", 9, "bold"), foreground="#0284c7").pack(side="left")

            btn_frame = ttk.Frame(ig_header)
            btn_frame.pack(side="right")

            if insta_text:
                def _copy_ig_text(text_to_copy=insta_text):
                    try:
                        self.clipboard_clear()
                        self.clipboard_append(text_to_copy)
                        self.log("📋 Instagram paylaşım metni panoya kopyalandı.")
                        messagebox.showinfo("Kopyalandı", "Instagram başlık, açıklama ve 5 hashtag panoya kopyalandı!")
                    except Exception as err:
                        self.log(f"Panoya kopyalama hatası: {err}")

                ttk.Button(btn_frame, text="📋 Metni Kopyala", command=_copy_ig_text).pack(side="left", padx=2)

            if cover_file:
                ttk.Button(
                    btn_frame,
                    text="🖼️ Kapak Fotoğrafını Aç",
                    command=lambda p=cover_file: self._open_path_in_os(p),
                ).pack(side="left", padx=2)

            def _regen_ig_package(t_dir=target_dir):
                try:
                    from services.social_media import prepare_instagram_package
                    from models.job import JobOrder, PROJECT_IKIZLER, PROJECT_DUYGU
                    is_ikiz = "ikizler" in t_dir.name.lower() or "masal" in t_dir.name.lower()
                    job_file = t_dir / "job.json"
                    job_obj = None
                    if job_file.exists():
                        try:
                            job_obj = JobOrder.from_json(job_file.read_text(encoding="utf-8"))
                        except Exception:
                            pass
                    if not job_obj:
                        job_obj = JobOrder(
                            project_name=PROJECT_IKIZLER if is_ikiz else PROJECT_DUYGU,
                            brand_name=t_dir.name,
                            brief=t_dir.name,
                            output_dir=str(t_dir),
                        )
                    d_map = {
                        "base": t_dir,
                        "scenarios": t_dir / "01_Senaryo",
                        "images": t_dir / "02_Gorseller",
                        "videos": t_dir / "03_Videolar",
                    }
                    prepare_instagram_package(job_obj, d_map, self.log)
                    self._render_review_scenes(t_dir)
                    messagebox.showinfo("Başarılı", "Instagram paylaşım paketi ve kapak fotoğrafı güncellendi!")
                except Exception as ex:
                    self.log(f"Instagram paketi oluşturma hatası: {ex}")
                    messagebox.showerror("Hata", f"İşlem sırasında hata oluştu:\n{ex}")

            ttk.Button(btn_frame, text="⚡ Paketi Yenile", command=_regen_ig_package).pack(side="left", padx=2)

            if insta_text:
                txt_widget = tk.Text(ig_card, height=6, wrap="word", font=("TkFixedFont", 9), relief="solid", borderwidth=1)
                txt_widget.insert("1.0", insta_text)
                txt_widget.configure(state="disabled")
                txt_widget.pack(fill="x", pady=4)

        if not self.review_current_scenes:
            ttk.Label(
                self.review_cards_frame,
                text="Bu klasörde görüntülenecek sahne bulunamadı.",
                font=("", 11, "italic"),
                foreground="#6b7280",
            ).pack(pady=20)
            return

        for sc in self.review_current_scenes:
            idx = sc.get("index", 1)
            title = sc.get("title", f"Sahne {idx}")
            dur = sc.get("duration_seconds", 5)
            voice = sc.get("voiceover_text", "")
            img_prompt = sc.get("image_prompt", "")
            vid_prompt = sc.get("video_prompt", "")

            card = ttk.LabelFrame(
                self.review_cards_frame,
                text=f"  🎬 Sahne {idx}: {title} ({dur} sn)  ",
                padding=8,
            )
            card.pack(fill="x", padx=4, pady=6)

            if voice:
                ttk.Label(
                    card,
                    text=f"🗣️ Seslendirme: {voice}",
                    wraplength=900,
                    font=("", 9, "italic"),
                    foreground="#4b5563",
                ).pack(anchor="w", padx=4, pady=(0, 6))

            cols = ttk.Frame(card)
            cols.pack(fill="x", expand=True)
            cols.columnconfigure(0, weight=1)
            cols.columnconfigure(1, weight=1)

            # --- SOL SÜTUN: GÖRSEL KONTROLÜ ---
            img_col = ttk.LabelFrame(cols, text="🖼️ Sahne Görseli", padding=6)
            img_col.grid(row=0, column=0, sticky="nsew", padx=(0, 4))

            img_file = images_dir / f"sahne_{idx}.png"
            has_img = img_file.exists() and img_file.stat().st_size > 20000
            # Eğer görsel henüz üretilmemişse varsayılan olarak seçili işaretle
            img_var = tk.BooleanVar(value=not has_img)
            self.regen_img_vars[idx] = img_var

            status_img_frame = ttk.Frame(img_col)
            status_img_frame.pack(fill="x", pady=(0, 4))

            if has_img:
                mb = img_file.stat().st_size / (1024 * 1024)
                ttk.Label(
                    status_img_frame,
                    text=f"✅ Görsel Mevcut ({mb:.2f} MB)",
                    foreground="#16a34a",
                    font=("", 9, "bold"),
                ).pack(side="left")
                ttk.Button(
                    status_img_frame,
                    text="👁️ Görseli Aç",
                    command=lambda p=img_file: self._open_path_in_os(p),
                ).pack(side="right")
                img_chk_label = "🔄 Bu Görseli Tekrar Üret"
            else:
                ttk.Label(
                    status_img_frame,
                    text="❌ Görsel Henüz Üretilmedi",
                    foreground="#dc2626",
                    font=("", 9, "bold"),
                ).pack(side="left")
                ttk.Button(
                    status_img_frame,
                    text="🚀 Bu Görseli Üret",
                    command=lambda i=idx: self.produce_single_scene_image(i),
                ).pack(side="right")
                img_chk_label = "🖼️ Bu Görseli Üret (Eksik)"

            bak_img = images_dir / f"sahne_{idx}.png.bak"
            if bak_img.exists():
                ttk.Label(
                    img_col,
                    text="📦 Önceki üretim yedeği (.bak) mevcut",
                    foreground="#6b7280",
                    font=("", 8),
                ).pack(anchor="w", pady=(0, 2))

            chk_img = ttk.Checkbutton(
                img_col,
                text=img_chk_label,
                variable=img_var,
            )
            chk_img.pack(anchor="w", pady=3)

            ttk.Label(img_col, text="Görsel Promptu (Düzenlenebilir):", font=("", 8, "bold")).pack(anchor="w", pady=(4, 1))
            img_txt = tk.Text(img_col, height=3, wrap="word", font=("", 9))
            img_txt.insert("1.0", img_prompt)
            img_txt.pack(fill="x", expand=True)
            self.review_img_text_widgets[idx] = img_txt

            # --- SAĞ SÜTUN: VİDEO KONTROLÜ ---
            vid_col = ttk.LabelFrame(cols, text="🎥 Sahne Videosu", padding=6)
            vid_col.grid(row=0, column=1, sticky="nsew", padx=(4, 0))

            vid_file = videos_dir / f"sahne_{idx}.mp4"
            has_vid = vid_file.exists() and vid_file.stat().st_size > 100000
            # Eğer video henüz üretilmemişse varsayılan olarak seçili işaretle
            vid_var = tk.BooleanVar(value=not has_vid)
            self.regen_vid_vars[idx] = vid_var

            status_vid_frame = ttk.Frame(vid_col)
            status_vid_frame.pack(fill="x", pady=(0, 4))

            if has_vid:
                mb = vid_file.stat().st_size / (1024 * 1024)
                ttk.Label(
                    status_vid_frame,
                    text=f"✅ Video Mevcut ({mb:.2f} MB)",
                    foreground="#16a34a",
                    font=("", 9, "bold"),
                ).pack(side="left")
                ttk.Button(
                    status_vid_frame,
                    text="▶️ Videoyu Oynat",
                    command=lambda p=vid_file: self._open_path_in_os(p),
                ).pack(side="right")
                vid_chk_label = "🔄 Bu Videoyu Tekrar Üret"
            else:
                if has_img:
                    ttk.Label(
                        status_vid_frame,
                        text="⚠️ Görsel Hazır, Video Bekliyor",
                        foreground="#d97706",
                        font=("", 9, "bold"),
                    ).pack(side="left")
                else:
                    ttk.Label(
                        status_vid_frame,
                        text="❌ Video Henüz Üretilmedi",
                        foreground="#dc2626",
                        font=("", 9, "bold"),
                    ).pack(side="left")
                ttk.Button(
                    status_vid_frame,
                    text="🚀 Bu Videoyu Üret",
                    command=lambda i=idx: self.produce_single_scene_video(i),
                ).pack(side="right")
                vid_chk_label = "🎬 Bu Videoyu Üret (Eksik)"

            bak_vid = videos_dir / f"sahne_{idx}.mp4.bak"
            if bak_vid.exists():
                ttk.Label(
                    vid_col,
                    text="📦 Önceki üretim yedeği (.bak) mevcut",
                    foreground="#6b7280",
                    font=("", 8),
                ).pack(anchor="w", pady=(0, 2))

            chk_vid = ttk.Checkbutton(
                vid_col,
                text=vid_chk_label,
                variable=vid_var,
            )
            chk_vid.pack(anchor="w", pady=3)

            ttk.Label(vid_col, text="Video Promptu (Düzenlenebilir):", font=("", 8, "bold")).pack(anchor="w", pady=(4, 1))
            vid_txt = tk.Text(vid_col, height=3, wrap="word", font=("", 9))
            vid_txt.insert("1.0", vid_prompt)
            vid_txt.pack(fill="x", expand=True)
            self.review_vid_text_widgets[idx] = vid_txt

    def _open_path_in_os(self, path: Path | str):
        p = Path(path)
        if p.exists():
            subprocess.Popen(["open", str(p)])
        else:
            messagebox.showwarning("Dosya Bulunamadı", f"Dosya mevcut değil:\n{p}")

    def _open_current_review_folder(self):
        name = self.review_folder_var.get().strip()
        if not name:
            self.open_outputs()
            return
        p = OUTPUTS_DIR / name
        if not p.exists():
            self.open_outputs()
            return
        self._open_path_in_os(p)

    def select_all_review_items(self, kind="both"):
        if kind in ("images", "both"):
            for v in self.regen_img_vars.values():
                v.set(True)
        if kind in ("videos", "both"):
            for v in self.regen_vid_vars.values():
                v.set(True)
        if kind == "images":
            for v in self.regen_vid_vars.values():
                v.set(False)
        if kind == "videos":
            for v in self.regen_img_vars.values():
                v.set(False)

    def select_missing_review_items(self):
        name = self.review_folder_var.get().strip()
        if not name:
            return
        target_dir = OUTPUTS_DIR / name
        images_dir = target_dir / "02_Gorseller"
        videos_dir = target_dir / "03_Videolar"

        for idx, var in self.regen_img_vars.items():
            img_p = images_dir / f"sahne_{idx}.png"
            var.set(not (img_p.exists() and img_p.stat().st_size > 20000))

        for idx, var in self.regen_vid_vars.items():
            vid_p = videos_dir / f"sahne_{idx}.mp4"
            var.set(not (vid_p.exists() and vid_p.stat().st_size > 100000))

    def clear_all_review_items(self):
        for v in self.regen_img_vars.values():
            v.set(False)
        for v in self.regen_vid_vars.values():
            v.set(False)

    def produce_single_scene_video(self, scene_idx: int):
        if self._running:
            messagebox.showwarning("Devam Eden İşlem", "Şu anda devam eden bir üretim süreci var. Lütfen tamamlanmasını bekleyin.")
            return
        self.clear_all_review_items()
        if scene_idx in self.regen_vid_vars:
            self.regen_vid_vars[scene_idx].set(True)
        self.regenerate_selected_scenes()

    def produce_single_scene_image(self, scene_idx: int):
        if self._running:
            messagebox.showwarning("Devam Eden İşlem", "Şu anda devam eden bir üretim süreci var. Lütfen tamamlanmasını bekleyin.")
            return
        self.clear_all_review_items()
        if scene_idx in self.regen_img_vars:
            self.regen_img_vars[scene_idx].set(True)
        self.regenerate_selected_scenes()

    def regenerate_selected_scenes(self):
        if self._running:
            messagebox.showwarning("Devam Eden İşlem", "Şu anda devam eden bir üretim süreci var. Lütfen tamamlanmasını bekleyin.")
            return

        name = self.review_folder_var.get().strip()
        if not name:
            messagebox.showwarning("Proje Seçilmedi", "Lütfen incelenecek ve tekrar üretilecek bir proje seçin.")
            return

        target_dir = OUTPUTS_DIR / name
        if not target_dir.exists():
            messagebox.showerror("Hata", f"Proje klasörü bulunamadı: {target_dir}")
            return

        images_dir = target_dir / "02_Gorseller"
        videos_dir = target_dir / "03_Videolar"
        images_dir.mkdir(parents=True, exist_ok=True)
        videos_dir.mkdir(parents=True, exist_ok=True)

        sel_imgs = [idx for idx, var in self.regen_img_vars.items() if var.get()]
        sel_vids = [idx for idx, var in self.regen_vid_vars.items() if var.get()]

        if not sel_imgs and not sel_vids:
            messagebox.showwarning(
                "Seçim Yapılmadı",
                "Lütfen üretilmesini veya tekrar üretilmesini istediğiniz en az bir sahne görseli veya sahne videosu seçin."
            )
            return

        new_vids = [idx for idx in sel_vids if not (videos_dir / f"sahne_{idx}.mp4").exists()]
        regen_vids = [idx for idx in sel_vids if (videos_dir / f"sahne_{idx}.mp4").exists()]
        new_imgs = [idx for idx in sel_imgs if not (images_dir / f"sahne_{idx}.png").exists()]
        regen_imgs = [idx for idx in sel_imgs if (images_dir / f"sahne_{idx}.png").exists()]

        msg_lines = ["Seçilen sahneler için üretim başlatılacak:\n"]
        if new_vids:
            msg_lines.append(f"• 🎬 İlk Kez Üretilecek Eksik Videolar: Sahne {', '.join(map(str, sorted(new_vids)))}")
        if regen_vids:
            msg_lines.append(f"• 🔄 Yeniden Üretilecek Videolar: Sahne {', '.join(map(str, sorted(regen_vids)))}")
        if new_imgs:
            msg_lines.append(f"• 🖼️ İlk Kez Üretilecek Eksik Görseller: Sahne {', '.join(map(str, sorted(new_imgs)))}")
        if regen_imgs:
            msg_lines.append(f"• 🔄 Yeniden Üretilecek Görseller: Sahne {', '.join(map(str, sorted(regen_imgs)))}")

        msg_lines.append("\n✅ Mevcut diğer tüm sahneler korunacak ve ardından tam video birleştirilecektir.")
        msg_lines.append("Devam etmek istiyor musunuz?")

        if not messagebox.askyesno("Üretim Onayı", "\n".join(msg_lines)):
            return

        # Prompt düzenlemelerini sahne planına kaydet
        scenarios_dir = target_dir / "01_Senaryo"
        plan_file = scenarios_dir / "sahne_plani.json"
        if plan_file.exists():
            try:
                scenes_data = json.loads(plan_file.read_text(encoding="utf-8"))
                for sc in scenes_data:
                    idx = sc.get("index")
                    if idx in self.review_img_text_widgets:
                        new_ip = self.review_img_text_widgets[idx].get("1.0", "end-1c").strip()
                        if new_ip:
                            sc["image_prompt"] = new_ip
                    if idx in self.review_vid_text_widgets:
                        new_vp = self.review_vid_text_widgets[idx].get("1.0", "end-1c").strip()
                        if new_vp:
                            sc["video_prompt"] = new_vp
                plan_file.write_text(json.dumps(scenes_data, ensure_ascii=False, indent=2), encoding="utf-8")
                self.log(f"📝 Sahne planı güncel promptlarla güncellendi: {plan_file.name}")
            except Exception as pe:
                self.log(f"Sahne planı kaydedilirken uyarı: {pe}")

        # Seçilen görselleri ve videoları yedekle (yeniden üretilebilmeleri için)
        images_dir = target_dir / "02_Gorseller"
        videos_dir = target_dir / "03_Videolar"

        for idx in sel_imgs:
            img_p = images_dir / f"sahne_{idx}.png"
            if img_p.exists():
                bak_p = images_dir / f"sahne_{idx}.png.bak"
                if bak_p.exists():
                    bak_p.unlink()
                img_p.rename(bak_p)
                self.log(f"🔄 [Tekrar Üretim] Sahne {idx} görseli yedeklendi: {bak_p.name}")
            orig_p = images_dir / f"sahne_{idx}.orig.jpg"
            if orig_p.exists():
                orig_bak = images_dir / f"sahne_{idx}.orig.jpg.bak"
                if orig_bak.exists():
                    orig_bak.unlink()
                orig_p.rename(orig_bak)

        for idx in sel_vids:
            vid_p = videos_dir / f"sahne_{idx}.mp4"
            if vid_p.exists():
                bak_p = videos_dir / f"sahne_{idx}.mp4.bak"
                if bak_p.exists():
                    bak_p.unlink()
                vid_p.rename(bak_p)
                self.log(f"🔄 [Tekrar Üretim] Sahne {idx} videosu yedeklendi: {bak_p.name}")

        # Tam videoyu ve teslimat bayrağını temizle ki yeniden birleştirilip teslim edilsin
        tam_vid = videos_dir / "tam_video.mp4"
        if tam_vid.exists():
            tam_vid.unlink()

        deliv_flag = target_dir / ".delivered"
        if deliv_flag.exists():
            deliv_flag.unlink()

        # Otomasyon yapılandırmasını oluştur
        cfg = self._get_current_job_cfg()
        cfg["output_dir"] = str(target_dir)
        cfg["scene_plan_source"] = "reused_existing"
        cfg["brand_name"] = name
        cfg["dry_run"] = False
        cfg["target_image_scenes"] = sel_imgs
        cfg["target_video_scenes"] = sel_vids

        refs = self.selected_refs
        self._running = True
        self.run_btn.configure(state="disabled")
        if self.stop_btn:
            self.stop_btn.configure(state="normal")
        if self.review_regen_btn:
            self.review_regen_btn.configure(state="disabled")
        if self.review_stop_btn:
            self.review_stop_btn.configure(state="normal")

        logger = SafeLogger(self.log, LOGS_DIR / "automation.log")
        now_str = datetime.now().strftime("%H:%M:%S")
        self.log(f"\n{'='*55}\n🚀 [SEÇİLEN SAHNELER İÇİN ÜRETİM BAŞLATILDI] ({now_str})\n"
                 f"Hedef Klasör: {target_dir.name}\n"
                 f"Görseller: {sel_imgs or 'Yok'} | Videolar: {sel_vids or 'Yok'}\n{'='*55}")

        def work():
            report = None
            try:
                report = Automation(cfg, logger).run(refs)
            except Exception as e:
                logger("HATA: " + str(e))
            finally:
                self.after(0, lambda: self._on_regen_finished(report, target_dir))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _on_regen_finished(self, report, target_dir: Path):
        self._running = False
        self.run_btn.configure(state="normal")
        if self.stop_btn:
            self.stop_btn.configure(state="disabled")
        if self.review_regen_btn:
            self.review_regen_btn.configure(state="normal")
        if self.review_stop_btn:
            self.review_stop_btn.configure(state="disabled")
        self.refresh_review_tab()

        if report is not None and report.overall_status == "success":
            self.log("🎉 [BAŞARILI] Seçilen sahneler başarıyla üretildi, tam video birleştirildi!")
            messagebox.showinfo("Başarılı", f"Seçilen sahneler başarıyla üretildi ve tam video güncellendi!\n\nProje: {target_dir.name}")
        elif report is not None and report.overall_status == "dry_run":
            self.log("ℹ️ [GÜVENLİ TEST] Simülasyon tamamlandı.")
        else:
            self.log("⚠️ [BEKLEMEDE] Tekrar üretim sürecinde bazı aşamalar tamamlanamadı veya limit nedeniyle durdu.")
            self.log("📌 Kalan sahneler için tekrar üretim başlatabilirsiniz.")

    def quick_merge_review_videos(self):
        name = self.review_folder_var.get().strip()
        if not name:
            messagebox.showwarning("Proje Seçilmedi", "Lütfen bir proje seçin.")
            return

        target_dir = OUTPUTS_DIR / name
        videos_dir = target_dir / "03_Videolar"
        if not videos_dir.exists():
            messagebox.showerror("Hata", f"Videolar klasörü bulunamadı: {videos_dir}")
            return

        vid_files = []
        for f in videos_dir.glob("sahne_*.mp4"):
            m = re.search(r"sahne_(\d+)\.mp4", f.name)
            if m:
                vid_files.append((int(m.group(1)), f))
        vid_files.sort(key=lambda x: x[0])
        valid_paths = [p for _, p in vid_files if p.stat().st_size > 1000]

        if not valid_paths:
            messagebox.showwarning("Video Bulunamadı", "Birleştirilecek en az 1 sahne videosu bulunamadı.")
            return

        if not messagebox.askyesno(
            "Video Birleştirme",
            f"{len(valid_paths)} sahne videosu FFmpeg ile 'tam_video.mp4' olarak birleştirilecek.\n\nDevam edilsin mi?"
        ):
            return

        try:
            from services.video_composer import merge_scene_videos
            out_file = videos_dir / "tam_video.mp4"
            if out_file.exists():
                out_file.unlink()

            self.log(f"🎬 {len(valid_paths)} sahne videosu birleştiriliyor…")
            composed = merge_scene_videos(valid_paths, out_file, self.log)
            if composed and composed.exists() and composed.stat().st_size > 1000:
                mb = composed.stat().st_size / (1024 * 1024)
                self.log(f"✅ Tam video hazır: {composed.name} ({mb:.2f} MB)")
                try:
                    from services.social_media import prepare_instagram_package
                    from models.job import JobOrder, PROJECT_IKIZLER, PROJECT_DUYGU
                    is_ikiz = "ikizler" in name.lower() or "masal" in name.lower()
                    job_file = target_dir / "job.json"
                    job_obj = None
                    if job_file.exists():
                        try:
                            job_obj = JobOrder.from_json(job_file.read_text(encoding="utf-8"))
                        except Exception:
                            pass
                    if not job_obj:
                        job_obj = JobOrder(
                            project_name=PROJECT_IKIZLER if is_ikiz else PROJECT_DUYGU,
                            brand_name=name,
                            brief=name,
                            output_dir=str(target_dir),
                        )
                    d_map = {
                        "base": target_dir,
                        "scenarios": target_dir / "01_Senaryo",
                        "images": target_dir / "02_Gorseller",
                        "videos": videos_dir,
                    }
                    prepare_instagram_package(job_obj, d_map, self.log)
                except Exception as ig_err:
                    self.log(f"Instagram paylaşım paketi uyarısı: {ig_err}")

                self.refresh_review_tab()
                messagebox.showinfo(
                    "Başarılı",
                    f"Tam video başarıyla oluşturuldu!\n\nDosya: {composed.name} ({mb:.2f} MB)\nKonum: {composed}\n\nInstagram paylaşım paketi ve kapak fotoğrafı da hazırlandı."
                )
            else:
                messagebox.showerror("Hata", "Video birleştirilemedi. FFmpeg kurulu olduğundan emin olun.")
        except Exception as e:
            self.log(f"Video birleştirme hatası: {e}")
            messagebox.showerror("Hata", f"Video birleştirme sırasında hata oluştu:\n{e}")

    # -------------------------------------------------------------------------
    # İş Kuyruğu & Durum Paneli (Queue Tab & Dashboard)
    # -------------------------------------------------------------------------
    def _build_queue_tab(self):
        # 1. Durum Kartı (Dashboard)
        status_box = ttk.LabelFrame(self.queue_tab, text="⚡ Sistem, Gemini Oturum ve Kota Durumu", padding=10)
        status_box.pack(fill="x", padx=4, pady=4)

        row1 = ttk.Frame(status_box)
        row1.pack(fill="x", pady=2)
        ttk.Label(row1, text="Motor Durumu: ", font=("TkDefaultFont", 11, "bold")).pack(side="left")
        self.queue_worker_status_lbl = ttk.Label(row1, text="⚪ Boşta (Hazır)", font=("TkDefaultFont", 11))
        self.queue_worker_status_lbl.pack(side="left", padx=(0, 20))

        ttk.Label(row1, text="Aktif İş: ", font=("TkDefaultFont", 11, "bold")).pack(side="left")
        self.queue_active_job_lbl = ttk.Label(row1, text="Yok", font=("TkDefaultFont", 11))
        self.queue_active_job_lbl.pack(side="left")

        row2 = ttk.Frame(status_box)
        row2.pack(fill="x", pady=2)
        ttk.Label(row2, text="Aktif Profil: ", font=("TkDefaultFont", 10, "bold")).pack(side="left")
        self.queue_profile_info_lbl = ttk.Label(row2, text="Varsayılan Oturum", font=("TkDefaultFont", 10))
        self.queue_profile_info_lbl.pack(side="left", padx=(0, 20))

        ttk.Label(row2, text="Kullanılan Gem: ", font=("TkDefaultFont", 10, "bold")).pack(side="left")
        self.queue_gem_info_lbl = ttk.Label(row2, text="-", font=("TkDefaultFont", 10))
        self.queue_gem_info_lbl.pack(side="left")

        row3 = ttk.Frame(status_box)
        row3.pack(fill="x", pady=2)
        ttk.Label(row3, text="Sonraki Deneme: ", font=("TkDefaultFont", 10, "bold")).pack(side="left")
        self.queue_next_retry_lbl = ttk.Label(row3, text="Bekleme yok", font=("TkDefaultFont", 10))
        self.queue_next_retry_lbl.pack(side="left", padx=(0, 20))

        ttk.Label(row3, text="Son Hata: ", font=("TkDefaultFont", 10, "bold")).pack(side="left")
        self.queue_last_error_lbl = ttk.Label(row3, text="Hata kaydı yok", font=("TkDefaultFont", 10))
        self.queue_last_error_lbl.pack(side="left")

        row4 = ttk.Frame(status_box)
        row4.pack(fill="x", pady=2)
        self.queue_counts_lbl = ttk.Label(row4, text="Kuyruk: Toplam: 0 | Bekleyen: 0 | Tamamlanan: 0", font=("TkDefaultFont", 10, "italic"))
        self.queue_counts_lbl.pack(side="left")

        # 2. Kuyruk Kontrolleri
        ctrl_frame = ttk.Frame(self.queue_tab, padding=(0, 4, 0, 6))
        ctrl_frame.pack(fill="x", padx=4)

        self.queue_start_btn = ttk.Button(ctrl_frame, text="▶️ Kuyruk Motorunu Başlat", command=self._start_queue_runner)
        self.queue_start_btn.pack(side="left", padx=(0, 6))

        self.queue_pause_btn = ttk.Button(ctrl_frame, text="⏸️ Kuyruğu Duraklat", command=self._pause_queue)
        self.queue_pause_btn.pack(side="left", padx=(0, 6))

        self.queue_resume_btn = ttk.Button(ctrl_frame, text="▶️ Devam Ettir", command=self._resume_queue)
        self.queue_resume_btn.pack(side="left", padx=(0, 6))

        self.queue_stop_btn = ttk.Button(ctrl_frame, text="⏹️ Motoru Durdur", command=self._stop_queue_runner)
        self.queue_stop_btn.pack(side="left", padx=(0, 6))

        self.queue_refresh_btn = ttk.Button(ctrl_frame, text="🔄 Yenile", command=self.refresh_queue_tab)
        self.queue_refresh_btn.pack(side="left", padx=(0, 6))

        self.queue_clean_btn = ttk.Button(ctrl_frame, text="🧹 Tamamlananları Temizle", command=self._clear_completed_queue)
        self.queue_clean_btn.pack(side="right")

        # 3. İş Tablosu (Treeview)
        table_frame = ttk.LabelFrame(self.queue_tab, text="📋 Kalıcı İş Kuyruğu (SQLite)", padding=6)
        table_frame.pack(fill="both", expand=True, padx=4, pady=4)

        cols = ("job_id", "brand", "type", "gem", "status", "scenes", "retry", "next_try", "error")
        self.queue_tree = ttk.Treeview(table_frame, columns=cols, show="headings", selectmode="browse")
        self.queue_tree.heading("job_id", text="İş ID")
        self.queue_tree.heading("brand", text="Marka / Başlık")
        self.queue_tree.heading("type", text="Tür")
        self.queue_tree.heading("gem", text="Gem")
        self.queue_tree.heading("status", text="Durum")
        self.queue_tree.heading("scenes", text="Sahneler")
        self.queue_tree.heading("retry", text="Deneme")
        self.queue_tree.heading("next_try", text="Sonraki Deneme")
        self.queue_tree.heading("error", text="Son Hata / Açıklama")

        self.queue_tree.column("job_id", width=75, anchor="center")
        self.queue_tree.column("brand", width=140, anchor="w")
        self.queue_tree.column("type", width=95, anchor="w")
        self.queue_tree.column("gem", width=120, anchor="w")
        self.queue_tree.column("status", width=110, anchor="center")
        self.queue_tree.column("scenes", width=75, anchor="center")
        self.queue_tree.column("retry", width=65, anchor="center")
        self.queue_tree.column("next_try", width=130, anchor="w")
        self.queue_tree.column("error", width=220, anchor="w")

        tree_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.queue_tree.yview)
        self.queue_tree.configure(yscrollcommand=tree_scroll.set)
        self.queue_tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")
        self.refresh_queue_tab()

    def refresh_queue_tab(self):
        try:
            status_data = self.queue_runner.get_status()
            summary = self.job_queue.get_queue_summary()
            counts = summary.get("counts", {})
            is_paused = summary.get("is_paused", False)
            is_running = self.queue_runner.is_running or self._running

            # Durum rozeti
            if is_paused:
                self.queue_worker_status_lbl.configure(text="⏸️ Duraklatıldı", foreground="#D97706")
            elif is_running:
                self.queue_worker_status_lbl.configure(text="🟢 Çalışıyor", foreground="#16A34A")
            elif counts.get("waiting", 0) > 0:
                self.queue_worker_status_lbl.configure(text="🟡 Beklemede (Kota/Tekrar)", foreground="#CA8A04")
            else:
                self.queue_worker_status_lbl.configure(text="⚪ Boşta (Bekliyor)", foreground="#4B5563")

            # Aktif iş
            cur_job = status_data.get("current_job")
            if cur_job:
                self.queue_active_job_lbl.configure(text=f"#{cur_job['job_id']} - {cur_job['brand_name']} ({cur_job['content_type']})")
            else:
                self.queue_active_job_lbl.configure(text="Yok (Yeni iş bekleniyor)")

            # Profil & Gem
            prof = self.profile_manager.get_active_profile()
            prof_status = "Açık" if prof.is_currently_usable() else ("Kotada" if prof.status == "rate_limited" else "Müdahale Gerekli")
            self.queue_profile_info_lbl.configure(text=f"{prof.display_name or prof.profile_key} ({prof.account_email or 'Yerel'}) | {prof_status}")

            gem_url = self.cfg.get("gem_url", "")
            gem_id = extract_gem_id_from_url(gem_url)
            gem_name = KNOWN_GEMS.get(gem_id, {}).get("name", gem_id or "Standart Gem")
            self.queue_gem_info_lbl.configure(text=f"{gem_name} ({gem_id})")

            # Sonraki deneme ve geri sayım
            earliest = summary.get("earliest_retry_at")
            if earliest:
                try:
                    dt = datetime.fromisoformat(earliest)
                    diff = (dt - datetime.now(timezone.utc)).total_seconds()
                    if diff > 0:
                        mins = int(diff // 60)
                        secs = int(diff % 60)
                        self.queue_next_retry_lbl.configure(text=f"{dt.strftime('%H:%M:%S UTC')} ({mins}dk {secs}sn kaldı)")
                    else:
                        self.queue_next_retry_lbl.configure(text="Süre doldu (Çalışmaya hazır)")
                except Exception:
                    self.queue_next_retry_lbl.configure(text=str(earliest))
            else:
                self.queue_next_retry_lbl.configure(text="Bekleme yok")

            # Kuyruk sayıları
            self.queue_counts_lbl.configure(
                text=f"Kuyruk: Toplam {counts.get('total', 0)} | Bekleyen: {counts.get('pending', 0)} | "
                     f"Beklemede: {counts.get('waiting', 0)} | Çalışan: {counts.get('running', 0)} | "
                     f"Tamamlanan: {counts.get('completed', 0)} | Müdahale Gerekli: {counts.get('needs_attention', 0)} | "
                     f"Hatalı: {counts.get('failed', 0)}"
            )

            # Tabloyu doldur
            for item in self.queue_tree.get_children():
                self.queue_tree.delete(item)

            jobs = self.job_queue.list_jobs(limit=100)
            status_map = {
                "completed": "✅ Tamamlandı",
                "running": "⚡ Çalışıyor",
                "waiting": "⏳ Beklemede",
                "pending": "🕒 Sırada",
                "needs_attention": "🛑 Müdahale Gerekli",
                "failed": "❌ Başarısız",
                "paused": "⏸️ Duraklatıldı",
            }
            for j in jobs:
                st_text = status_map.get(j["status"], j["status"])
                # Sahneler (Görsel ve Video ilerlemesini birlikte kontrol et)
                comp_scenes = len(json.loads(j.get("completed_scenes") or "[]"))
                total_scenes = j.get("scene_count") or 1
                pend_scenes = json.loads(j.get("pending_scenes") or "[]")
                comp_imgs = sum(1 for s in pend_scenes if s.get("status") in ("image_done", "video_done", "completed") or s.get("image_path"))

                out_dir = Path(j.get("output_dir", "")) if j.get("output_dir") else None
                if out_dir and out_dir.exists():
                    img_files = list((out_dir / "02_Gorseller").glob("sahne_*.png")) if (out_dir / "02_Gorseller").exists() else []
                    vid_files = list((out_dir / "03_Videolar").glob("sahne_*.mp4")) if (out_dir / "03_Videolar").exists() else []
                    comp_imgs = max(comp_imgs, len(img_files))
                    comp_scenes = max(comp_scenes, len(vid_files))

                if comp_scenes > 0:
                    scenes_str = f"{comp_scenes}/{total_scenes} Video"
                elif comp_imgs > 0:
                    scenes_str = f"{comp_imgs}/{total_scenes} Görsel"
                else:
                    scenes_str = f"0/{total_scenes} Planlandı"

                # Deneme durumu
                retries_done = j.get("retry_count", 0)
                max_retries = j.get("max_retries", 3)
                if j.get("status") == "running":
                    retry_str = f"{retries_done}/{max_retries} (Aktif)"
                elif j.get("status") == "waiting":
                    retry_str = f"{retries_done}/{max_retries} (Beklemede)"
                else:
                    retry_str = f"{retries_done}/{max_retries}"

                # Gem adı
                g_id = j.get("gem_id") or extract_gem_id_from_url(j.get("gem_url", ""))
                g_title = KNOWN_GEMS.get(g_id, {}).get("name", g_id or "-")

                self.queue_tree.insert(
                    "",
                    "end",
                    values=(
                        j["job_id"],
                        j["brand_name"],
                        j["content_type"],
                        g_title,
                        st_text,
                        scenes_str,
                        retry_str,
                        j.get("next_retry_at") or "-",
                        j.get("last_error") or "-",
                    ),
                )
        except Exception:
            pass

    def _periodic_queue_refresh(self):
        try:
            if self.winfo_exists():
                self.refresh_queue_tab()
                self.after(2000, self._periodic_queue_refresh)
        except Exception:
            pass

    def _start_queue_runner(self):
        self.queue_runner.start()
        self.refresh_queue_tab()
        self.log("▶️ Kuyruk motoru başlatıldı.")

    def _pause_queue(self):
        self.queue_runner.pause()
        self.refresh_queue_tab()
        self.log("⏸️ Kuyruk duraklatıldı. Mevcut işlem bitiminde yeni işe geçilmeyecek.")

    def _resume_queue(self):
        self.queue_runner.resume()
        self.refresh_queue_tab()
        self.log("▶️ Kuyruk devam ettirildi.")

    def _stop_queue_runner(self):
        self.stop_production()

    def _clear_completed_queue(self):
        try:
            with self.job_queue._get_connection() as conn:
                conn.execute("DELETE FROM jobs WHERE status = 'completed'")
            self.refresh_queue_tab()
            self.log("🧹 Tamamlanmış işler kuyruktan temizlendi.")
        except Exception as e:
            self.log(f"Kuyruk temizleme hatası: {e}")

    def destroy(self):
        try:
            self._save_pending_job()
        except Exception:
            pass
        try:
            if hasattr(self, "queue_runner") and self.queue_runner:
                self.queue_runner.stop(timeout=1.0)
        except Exception:
            pass
        super().destroy()


if __name__ == "__main__":
    App().mainloop()
