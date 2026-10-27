from __future__ import annotations

from pathlib import Path


class FakeProvider:
    def __init__(
        self,
        *,
        connect_error=None,
        video_error=None,
        video_errors=None,
        image_error=None,

        upload_error=None,
        upload_verified=True,
        new_video="https://example.invalid/new-video.mp4",
        new_videos=None,
        new_image="https://example.invalid/new-image.png",
        old_videos=None,
        old_images=None,
        response_text=None,
    ):
        self.connect_error = connect_error
        self.video_error = video_error
        self.video_errors = list(video_errors) if video_errors is not None else None
        self.image_error = image_error

        self.upload_error = upload_error
        self.upload_verified = upload_verified
        self.new_video = new_video
        self.new_videos = list(new_videos) if new_videos else []
        self.new_image = new_image
        self.old_videos = set(old_videos or ["https://example.invalid/old-video.mp4"])
        self.old_images = set(old_images or ["https://example.invalid/old-image.png"])
        self.response_text = response_text if response_text is not None else (
            "[SAHNE 1]\nBAŞLIK: Test Sahnesi\nAÇIKLAMA: Test sahne açıklaması\n"
            "GÖRSEL_PROMPT: Test görseli\nVIDEO_PROMPT: Test video promptu\nSESLENDIRME: Test seslendirmesi"
        )
        self.calls: list[str] = []
        self.sent_prompts: list[str] = []
        self.sent_prompt = None
        self.attached = None
        self.old_sources_seen = None

    def connect(self):
        self.calls.append("connect")
        if self.connect_error:
            raise self.connect_error

    def reset_chat_session(self):
        self.calls.append("reset_chat_session")

    def snapshot_videos(self):
        self.calls.append("snapshot_videos")
        return set(self.old_videos)

    def snapshot_images(self):
        self.calls.append("snapshot_images")
        return set(self.old_images)

    def attach_references(self, refs):
        self.calls.append("attach_references")
        self.attached = list(refs)

    def send_prompt(self, prompt):
        self.calls.append("send_prompt")
        self.sent_prompt = prompt
        self.sent_prompts.append(prompt)

    def wait_for_new_image(self, timeout_minutes=2, old_sources=None, initial_count=0, **kwargs):
        self.calls.append("wait_for_new_image")
        if self.image_error:
            raise self.image_error
        return self.new_image

    def save_latest_generated_image(self, dest_path: str | Path):
        self.calls.append("save_latest_generated_image")
        p = Path(dest_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"dummy_generated_image_bytes")
        return True

    def get_image_count(self) -> int:
        self.calls.append("get_image_count")
        return len(self.calls)

    def wait_until_idle(self, timeout_seconds=30):
        self.calls.append("wait_until_idle")

    def wait_for_new_response_text(self, timeout_seconds=60, **kwargs):
        self.calls.append("wait_for_new_response_text")
        return self.response_text

    def wait_for_new_video(self, timeout_minutes, old_sources):
        self.calls.append("wait_for_new_video")
        self.old_sources_seen = set(old_sources)
        if self.video_errors:
            err = self.video_errors.pop(0)
            if err:
                raise err
        if self.video_error:
            raise self.video_error
        if self.new_videos:
            return self.new_videos.pop(0)
        if self.new_video in old_sources:
            raise TimeoutError("Eski video yeni kabul edilmedi.")
        return self.new_video


    def download_file(self, media_src: str, dest_path: str | Path):
        self.calls.append("download_file")
        p = Path(dest_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"dummy_media_bytes")
        return True

    def read_response_text(self):
        self.calls.append("read_response_text")
        return self.response_text

    def upload_to_drive(self, video_src):
        self.calls.append("upload_to_drive")
        if self.upload_error:
            raise self.upload_error
        return {
            "filename": "DuyguAI_video_test.mp4",
            "verified": self.upload_verified,
            "bytes": 12,
            "src": video_src,
        }

    def upload_folder_to_drive(self, folder_path: str | Path):
        self.calls.append("upload_folder_to_drive")
        if self.upload_error:
            raise self.upload_error
        return {
            "folder": Path(folder_path).name,
            "verified": self.upload_verified,
            "path": str(folder_path),
        }

    def upload_all_outputs_to_drive(self, dirs: dict):
        self.calls.append("upload_all_outputs_to_drive")
        if self.upload_error:
            raise self.upload_error
        folders = [Path(p).name for p in [dirs.get("scenarios"), dirs.get("images"), dirs.get("videos")] if p]
        base_name = Path(dirs.get("base", "DuyguAI")).name if dirs.get("base") else "DuyguAI"
        return {
            "folder": base_name,
            "folders": folders,
            "verified": self.upload_verified,
        }


    def close(self):
        self.calls.append("close")
