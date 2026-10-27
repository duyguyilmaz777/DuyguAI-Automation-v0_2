import json
import pytest
from web_server import JOB_MANAGER, check_chrome_status, load_config, save_config
from models.job import PROJECT_DUYGU, PROJECT_IKIZLER

def test_load_and_save_config(tmp_path, monkeypatch):
    test_cfg_file = tmp_path / "config.json"
    monkeypatch.setattr("web_server.CONFIG_FILE", test_cfg_file)

    cfg = load_config()
    assert "selected_project" in cfg

    cfg["selected_project"] = PROJECT_IKIZLER
    save_config(cfg)

    loaded = load_config()
    assert loaded["selected_project"] == PROJECT_IKIZLER

def test_job_state_manager():
    manager = JOB_MANAGER
    manager.start_job(PROJECT_DUYGU, "job_test_1", dry_run=True)
    assert manager.is_running is True
    assert manager.current_job_id == "job_test_1"

    manager.add_log("INFO", "=== [1. Aşama] Senaryo ve Sahne Ayrıştırma Başlatılıyor ===")
    assert manager.current_stage == "stage_1_script"

    manager.add_log("INFO", "=== [2. Aşama] Sırayla Sahne Görselleri Üretiliyor ===")
    assert manager.current_stage == "stage_2_images"

    manager.add_log("INFO", "=== [3. Aşama] Sırayla Sahne Videoları Üretiliyor ===")
    assert manager.current_stage == "stage_3_videos"

    manager.finish_job(success=True, message="Test tamamlandı")
    assert manager.is_running is False
    assert manager.current_stage == "completed"

def test_check_chrome_status_invalid_port():
    # Geçersiz bir porta bağlanınca patlamadan offline dönmeli
    res = check_chrome_status("http://127.0.0.1:54321")
    assert res["online"] is False
    assert res["gemini_tabs_count"] == 0
