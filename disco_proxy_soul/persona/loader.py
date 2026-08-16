"""Load persona packages from disk.

A persona package is intentionally file-based so private personas can live
outside a public/open-source host application.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from .schema import (
    DOC_MODES,
    PersonaCharacter,
    PersonaDocument,
    PersonaPackage,
)

IDENTITY_FILES = {"identity.md", "persona.md", "voice.md"}
RESERVED_META = {
    "companion_name",
    "partner_name",
    "always_on_docs",
    "layers",
    "character",
    "example_lines",
}
FOLDER_MODES = {
    "always": "always_on",
    "always_on": "always_on",
    "presence": "presence",
    "author": "author",
    "docs": "author",
}
PRESENCE_FILENAMES = {"intimate-presence.md", "intimate.md", "presence.md"}
MODE_ALIASES = {
    "intimate": "presence",
    "intimate_presence": "presence",
    "intimate-presence": "presence",
}
DEFAULTS_DIR = Path(__file__).resolve().parent.parent / "defaults"


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


def _read_manifest(root: Path) -> dict[str, Any]:
    for name in ("manifest.json", "persona.json"):
        path = root / name
        if path.exists():
            return _read_json(path)
    return {}


def _read_identity(root: Path) -> str:
    if (root / "identity.md").exists():
        return _read_text(root / "identity.md", required=True)
    if (root / "persona.md").exists():
        return _read_text(root / "persona.md", required=True)
    raise PersonaLoadError(
        f"Required identity file missing: {root / 'identity.md'} "
        f"(or legacy {root / 'persona.md'})"
    )


def _normalize_mode(raw: Any) -> str | None:
    if not raw:
        return None
    mode = str(raw).strip().lower().replace("-", "_")
    mode = MODE_ALIASES.get(mode, mode)
    if mode in DOC_MODES:
        return mode
    return None


def _pretty_title(name: str) -> str:
    return name.replace(".md", "").replace("-", " ").replace("_", " ").title()


def _layer_index(layers: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(layers, dict):
        return {}
    index: dict[str, dict[str, Any]] = {}
    for key, value in layers.items():
        if not isinstance(value, dict):
            value = {"mode": value}
        posix = str(key).replace("\\", "/").lstrip("./")
        index[posix] = value
        index[Path(posix).name] = value
    return index


def _layer_for(path: Path, root: Path, index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rel = path.relative_to(root).as_posix()
    return index.get(rel) or index.get(path.name) or {}


def _mode_search_dirs(root: Path) -> list[Path]:
    docs = root / "docs"
    return [
        root,
        docs,
        *(docs / folder for folder in FOLDER_MODES),
    ]


def _collect_markdown(root: Path, layer_index: dict[str, dict[str, Any]]) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen or not path.is_file():
            return
        seen.add(resolved)
        found.append(path)

    docs_dir = root / "docs"
    if docs_dir.is_dir():
        for path in sorted(docs_dir.glob("*.md")):
            add(path)
        for folder in FOLDER_MODES:
            sub = docs_dir / folder
            if sub.is_dir():
                for path in sorted(sub.glob("*.md")):
                    add(path)
    for path in sorted(root.glob("*.md")):
        if path.name.lower() not in IDENTITY_FILES:
            add(path)
    for key in layer_index:
        add(root / key)
    return found


def _folder_mode(path: Path, root: Path) -> str | None:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return None
    if len(parts) >= 3 and parts[0] == "docs":
        return FOLDER_MODES.get(parts[1].lower())
    return None


def _resolve_mode(
    path: Path,
    root: Path,
    layer: dict[str, Any],
    always_on: set[str],
) -> str:
    explicit = _normalize_mode(layer.get("mode"))
    if explicit:
        return explicit
    if path.name in always_on:
        return "always_on"
    if path.name.lower() in PRESENCE_FILENAMES:
        return "presence"
    from_folder = _folder_mode(path, root)
    if from_folder:
        return from_folder
    return "author"


def _load_character(meta: dict[str, Any]) -> PersonaCharacter:
    fields: dict[str, str] = {}
    block = meta.get("character")
    if isinstance(block, dict):
        for key, value in block.items():
            if value is None or isinstance(value, (list, dict)):
                continue
            text = str(value).strip()
            if text:
                fields[str(key)] = text
    for key, value in meta.items():
        if key in RESERVED_META or key in fields:
            continue
        if isinstance(value, (str, int, float)):
            text = str(value).strip()
            if text:
                fields[str(key)] = text
    examples = meta.get("example_lines") or []
    if not isinstance(examples, list):
        examples = []
    return PersonaCharacter(
        fields=fields,
        example_lines=tuple(str(item).strip() for item in examples if str(item).strip()),
    )


def _assert_layer_files(root: Path, index: dict[str, dict[str, Any]]) -> None:
    checked: set[str] = set()
    search = _mode_search_dirs(root)
    for key in index:
        posix = str(key).replace("\\", "/")
        if posix in checked:
            continue
        checked.add(posix)
        path = root / posix
        if path.exists():
            continue
        name = Path(posix).name
        if "/" not in posix and any((directory / name).exists() for directory in search):
            continue
        raise PersonaLoadError(f"Persona layer file missing: {path}")


def _load_documents(
    root: Path,
    meta: dict[str, Any],
) -> tuple[PersonaDocument, ...]:
    always_on = {
        str(name) for name in meta.get("always_on_docs", []) if isinstance(name, str)
    }
    index = _layer_index(meta.get("layers"))
    _assert_layer_files(root, index)
    documents: list[PersonaDocument] = []
    for path in _collect_markdown(root, index):
        layer = _layer_for(path, root, index)
        tags_raw = layer.get("tags") or []
        if not isinstance(tags_raw, list):
            tags_raw = [tags_raw]
        title = str(layer.get("title") or "").strip() or _pretty_title(path.name)
        documents.append(
            PersonaDocument(
                name=path.name,
                content=path.read_text(encoding="utf-8"),
                path=path,
                mode=_resolve_mode(path, root, layer, always_on),
                tags=tuple(str(tag).strip() for tag in tags_raw if str(tag).strip()),
                title=title,
            )
        )
    return tuple(documents)


def apply_host_defaults(package: PersonaPackage) -> PersonaPackage:
    """Fill craft gaps from host defaults. User files always win."""
    voice = package.voice.strip()
    voice_is_default = False
    if not voice:
        voice = _read_text(DEFAULTS_DIR / "voice.md").strip()
        voice_is_default = bool(voice)

    documents = list(package.documents)
    uses_default_presence = False
    if not any(doc.mode == "presence" for doc in documents):
        presence_path = DEFAULTS_DIR / "presence.md"
        presence = _read_text(presence_path).strip()
        if presence:
            documents.append(
                PersonaDocument(
                    name="presence.md",
                    content=presence,
                    path=presence_path,
                    mode="presence",
                    title="Presence",
                    origin="host",
                )
            )
            uses_default_presence = True

    always_on = tuple(
        dict.fromkeys(
            list(package.always_on_docs)
            + [doc.name for doc in documents if doc.mode == "always_on"]
        )
    )
    return replace(
        package,
        voice=voice,
        documents=tuple(documents),
        always_on_docs=always_on,
        room_note=_read_text(DEFAULTS_DIR / "room.md").strip(),
        voice_is_default=voice_is_default,
        uses_default_presence=uses_default_presence,
    )


def load_persona(root: str | Path, persona_id: str | None = None) -> PersonaPackage:
    root_path = Path(root)
    if not root_path.exists():
        raise PersonaLoadError(f"Persona directory missing: {root_path}")
    if not root_path.is_dir():
        raise PersonaLoadError(f"Persona path is not a directory: {root_path}")

    resolved_id = persona_id or root_path.name
    meta = _read_manifest(root_path)
    always_on = meta.get("always_on_docs", [])
    if not isinstance(always_on, list):
        always_on = []
    documents = _load_documents(root_path, meta)
    derived_always_on = tuple(
        dict.fromkeys(
            [str(name) for name in always_on]
            + [doc.name for doc in documents if doc.mode == "always_on"]
        )
    )
    package = PersonaPackage(
        persona_id=resolved_id,
        root=root_path,
        identity=_read_identity(root_path),
        companion_name=str(meta.get("companion_name") or resolved_id),
        partner_name=str(meta.get("partner_name") or "you"),
        voice=_read_text(root_path / "voice.md"),
        facts_seed=_read_json(root_path / "facts.seed.json"),
        documents=documents,
        always_on_docs=derived_always_on,
        character=_load_character(meta),
    )
    return apply_host_defaults(package)
