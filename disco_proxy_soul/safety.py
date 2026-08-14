"""Prompt-injection guard for inbound user text and outbound model replies.

Base models can confabulate fake <system_warning>/<ctx_interruption> blocks
mid-generation. Outgoing replies are stripped. Incoming user text is defanged
so a quoted tag cannot act as a live instruction.
"""

from __future__ import annotations

import re
import sys
from typing import Any

_INJECTION_TAGS = (
    "system_warning",
    "ctx_interruption",
    "system-warning",
    "ctx-interruption",
)

_INJECTION_BLOCK_RE = re.compile(
    r"<\s*(?:" + "|".join(_INJECTION_TAGS) + r")\b[^>]*>.*?</\s*(?:"
    + "|".join(_INJECTION_TAGS) + r")\s*>",
    re.IGNORECASE | re.DOTALL,
)
_INJECTION_STRAY_TAG_RE = re.compile(
    r"</?\s*(?:" + "|".join(_INJECTION_TAGS) + r")\b[^>]*/?>",
    re.IGNORECASE,
)
_INJECTION_OPEN_TO_END_RE = re.compile(
    r"<\s*(?:" + "|".join(_INJECTION_TAGS) + r")\b[^>]*>.*\Z",
    re.IGNORECASE | re.DOTALL,
)


def sanitize_outgoing(text: str) -> str:
    """Strip fake system-instruction blocks from a model reply."""
    if not isinstance(text, str) or "<" not in text:
        return text
    cleaned, n = _INJECTION_BLOCK_RE.subn("", text)
    cleaned, t = _INJECTION_OPEN_TO_END_RE.subn("", cleaned)
    cleaned, m = _INJECTION_STRAY_TAG_RE.subn("", cleaned)
    if n or m or t:
        print(
            f"[injection-guard] outgoing: stripped {n} block(s), "
            f"{t} open-to-end tail(s), {m} stray tag(s)",
            file=sys.stderr,
        )
    return cleaned


def _defang_tag(match: re.Match[str]) -> str:
    return match.group(0).replace("<", "⟨").replace(">", "⟩")


def sanitize_incoming_text(text: str) -> str:
    """Defang fake system-instruction tags in user text (preserve visibility)."""
    if not isinstance(text, str) or "<" not in text:
        return text
    cleaned, n = _INJECTION_STRAY_TAG_RE.subn(_defang_tag, text)
    if n:
        print(
            f"[injection-guard] incoming: defanged {n} tag(s) in user content",
            file=sys.stderr,
        )
    return cleaned


def sanitize_incoming_content(content: Any) -> Any:
    """Defang injection tags in either a string or a list of content blocks."""
    if isinstance(content, str):
        return sanitize_incoming_text(content)
    if isinstance(content, list):
        return [
            (
                {**block, "text": sanitize_incoming_text(block["text"])}
                if isinstance(block, dict) and block.get("type") == "text" and "text" in block
                else block
            )
            for block in content
        ]
    return content
