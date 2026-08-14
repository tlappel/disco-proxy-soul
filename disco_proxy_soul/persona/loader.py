"""Load persona packages from disk.

A persona package is intentionally file-based so private personas can live
outside a public/open-source host application.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .schema import PersonaDocument, PersonaPackage


class PersonaLoadError(RuntimeError):
    """Raised when a persona package is missing required material."""


def _read_text(path: Path, required: bool = False) -> str:
    if not path.exists():
        if required:
            raise PersonaLoadError(f"Required persona file missing: {path}")
        return ""
    return path.read_text(encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise PersonaLoadError(f"Persona JSON must be an object: {path}")
    return data


def _load_documents(root: Path) -> tuple[PersonaDocument, ...]:
    docs_dir = root / "docs"
    if not docs_dir.exists():
        return ()
    documents: list[PersonaDocument] = []
    for path in sorted(docs_dir.glob("*.md")):
        documents.append(
            PersonaDocument(
                name=path.name,
                content=path.read_text(encoding="utf-8"),
                path=path,
            )
        )
    return tuple(documents)


def load_persona(root: str | Path, persona_id: str | None = None) -> PersonaPackage:
    root_path = Path(root)
    if not root_path.exists():
        raise PersonaLoadError(f"Persona directory missing: {root_path}")
    if not root_path.is_dir():
        raise PersonaLoadError(f"Persona path is not a directory: {root_path}")

    resolved_id = persona_id or root_path.name
    meta = _read_json(root_path / "persona.json")
    always_on = meta.get("always_on_docs", [])
    if not isinstance(always_on, list):
        always_on = []
    return PersonaPackage(
        persona_id=resolved_id,
        root=root_path,
        identity=_read_text(root_path / "persona.md", required=True),
        companion_name=str(meta.get("companion_name") or resolved_id),
        partner_name=str(meta.get("partner_name") or "you"),
        voice=_read_text(root_path / "voice.md"),
        facts_seed=_read_json(root_path / "facts.seed.json"),
        memory_policy=_read_json(root_path / "memory_policy.json"),
        documents=_load_documents(root_path),
        always_on_docs=tuple(str(name) for name in always_on),
    )
