from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

_URL_RE = re.compile(r"https?://[^\s]+", re.I)
_SECRETISH_RE = re.compile(
    r"(?i)\b(cookie|authorization|bearer|session|set-cookie|api[_-]?key)\b(?:[=:\s]+\S+)+"
)


def sanitize_log_line(message: str) -> str:
    text = str(message)
    text = _SECRETISH_RE.sub(lambda m: m.group(1) + "=***", text)

    def _strip_query(match: re.Match[str]) -> str:
        url = match.group(0)
        base = url.split("?", 1)[0]
        return base + ("?…" if "?" in url else "")

    return _URL_RE.sub(_strip_query, text)


class SafeLogger:
    def __init__(self, sink, log_path: Path | None = None):
        self._sink = sink
        self._log_path = log_path
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, message: str) -> None:
        line = sanitize_log_line(message)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        formatted = f"[{stamp}] {line}"
        self._sink(formatted)
        if self._log_path:
            with self._log_path.open("a", encoding="utf-8") as handle:
                handle.write(formatted + "\n")
