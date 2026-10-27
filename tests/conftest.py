import pytest
import tempfile
import shutil
from pathlib import Path
import services.workflow as wf


@pytest.fixture(autouse=True)
def isolate_test_directories(monkeypatch):
    """Her test için izole geçici klasör açar ve test bitiminde otomatik olarak temizler."""
    temp_dir = tempfile.mkdtemp(prefix="pytest_isolated_")
    temp_outputs = Path(temp_dir) / "outputs"
    temp_jobs = Path(temp_dir) / "jobs"
    temp_outputs.mkdir(parents=True, exist_ok=True)
    temp_jobs.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(wf, "OUTPUTS_DIR", temp_outputs)
    monkeypatch.setattr(wf, "JOBS_DIR", temp_jobs)

    yield {
        "root": Path(temp_dir),
        "outputs": temp_outputs,
        "jobs": temp_jobs,
    }

    shutil.rmtree(temp_dir, ignore_errors=True)
