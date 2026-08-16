"""Atomic JSON persistence used by the file memory backend.

Crash-safe writes (tmp + replace). Corrupt files are quarantined instead of
silently replaced with an empty default.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any


def ensure_data_dir(data_dir: str) -> None:
    os.makedirs(data_dir, exist_ok=True)


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def load_json(path: str, default: Any) -> Any:
    _ensure_parent(path)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception as exc:
            quarantine = f"{path}.corrupt-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            try:
                os.replace(path, quarantine)
                print(f"[memory] LOAD FAILED {path}: {exc} — quarantined to {quarantine}")
            except Exception as exc2:
                print(f"[memory] LOAD FAILED {path}: {exc} — quarantine failed too: {exc2}")
    return default


def save_json(path: str, data: Any) -> None:
    _ensure_parent(path)
    try:
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        os.replace(tmp_path, path)
    except Exception as exc:
        print(f"[memory] Save error {path}: {exc}")


def parse_llm_json(text: str) -> Any:
    """Extract the first JSON object/array from a model response."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\[{]", text):
        try:
            obj, _ = decoder.raw_decode(text[match.start():])
            return obj
        except json.JSONDecodeError:
            continue

    raise json.JSONDecodeError("No valid JSON object or array found", text, 0)


def normalize_memory_data(data: dict[str, Any] | Any, fallback_summary: str) -> dict[str, Any]:
    """Keep malformed memory fields from breaking save/moments paths."""
    if not isinstance(data, dict):
        data = {}

    summary = data.get("summary") or fallback_summary
    tags = data.get("tags", [])
    if isinstance(tags, str):
        tags = [tags]
    elif not isinstance(tags, list):
        tags = []
    tags = [str(tag) for tag in tags if tag]

    try:
        significance = float(data.get("significance", 0.7))
    except (TypeError, ValueError):
        significance = 0.7
    significance = max(0.0, min(1.0, significance))

    return {
        "summary": str(summary).strip(),
        "tags": tags,
        "significance": significance,
    }
