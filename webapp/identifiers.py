from __future__ import annotations

import re
from pathlib import Path


CLIP_ID_RE = re.compile(r"clip-(\d{3,})")


def validate_clip_id(value: object) -> str:
    if (not isinstance(value, str) or not value
            or value.startswith(".") or "/" in value or "\\" in value
            or Path(value).name != value or not CLIP_ID_RE.fullmatch(value)):
        raise ValueError(f"invalid clip id: {value!r}")
    return value
