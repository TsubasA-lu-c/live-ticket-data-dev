#!/usr/bin/env python3
"""Apply narrowly-scoped agent data patches safely.

Patch format:
{
  "operations": [
    {"action":"merge","file":"data/artist/foo.json","collection":"performances","id":"...","changes":{...}},
    {"action":"upsert","file":"data/artist/foo.json","collection":"tours","value":{...}},
    {"action":"delete","file":"data/artist/foo.json","collection":"lotteries","id":"..."},
    {"action":"top_merge","file":"data/artist/foo.json","changes":{...}},
    {"action":"text_replace","file":"COLLECTION_RULES.md","old":"...","new":"..."}
  ]
}

Safety rules:
- JSON data operations may modify only data/artist/*.json.
- text_replace may modify only COLLECTION_RULES.md and the old text must match exactly once.
- merge/delete must match exactly one existing object by id.
- upsert replaces an existing same-id object or appends a new one.
- JSON is rewritten with stable pretty formatting.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWED_ROOT = (ROOT / "data" / "artist").resolve()
ALLOWED_TEXT_FILES = {(ROOT / "COLLECTION_RULES.md").resolve()}


def fail(message: str) -> None:
    raise SystemExit(f"[ERROR] {message}")


def resolve_data_file(raw: str) -> Path:
    path = (ROOT / raw).resolve()
    if path.parent != ALLOWED_ROOT or path.suffix != ".json":
        fail(f"disallowed patch target: {raw}")
    if not path.exists():
        fail(f"patch target does not exist: {raw}")
    return path


def resolve_text_file(raw: str) -> Path:
    path = (ROOT / raw).resolve()
    if path not in ALLOWED_TEXT_FILES:
        fail(f"disallowed text patch target: {raw}")
    if not path.exists():
        fail(f"text patch target does not exist: {raw}")
    return path


def find_by_id(items: list, object_id: str) -> tuple[int, dict] | None:
    matches = [(i, item) for i, item in enumerate(items) if isinstance(item, dict) and item.get("id") == object_id]
    if len(matches) > 1:
        fail(f"duplicate id already exists: {object_id}")
    return matches[0] if matches else None


def main() -> int:
    patch_path = ROOT / (sys.argv[1] if len(sys.argv) > 1 else "agent/patches/current.json")
    if not patch_path.exists():
        fail(f"patch file not found: {patch_path.relative_to(ROOT)}")

    spec = json.loads(patch_path.read_text(encoding="utf-8"))
    operations = spec.get("operations")
    if not isinstance(operations, list):
        fail("patch spec must contain operations[]")

    cache: dict[Path, dict] = {}
    text_cache: dict[Path, str] = {}
    touched: set[Path] = set()
    text_touched: set[Path] = set()

    for n, op in enumerate(operations, 1):
        if not isinstance(op, dict):
            fail(f"operation #{n} is not an object")
        action = op.get("action")

        if action == "text_replace":
            path = resolve_text_file(op.get("file", ""))
            old = op.get("old")
            new = op.get("new")
            if not isinstance(old, str) or not isinstance(new, str) or not old:
                fail(f"operation #{n}: text_replace requires non-empty old and string new")
            text = text_cache.setdefault(path, path.read_text(encoding="utf-8"))
            count = text.count(old)
            if count != 1:
                fail(f"operation #{n}: text_replace expected exactly one match, found {count}")
            text_cache[path] = text.replace(old, new, 1)
            text_touched.add(path)
            continue

        path = resolve_data_file(op.get("file", ""))
        data = cache.setdefault(path, json.loads(path.read_text(encoding="utf-8")))

        if action == "top_merge":
            changes = op.get("changes")
            if not isinstance(changes, dict):
                fail(f"operation #{n}: top_merge changes must be object")
            data.update(changes)

        elif action in {"merge", "upsert", "delete"}:
            collection = op.get("collection")
            if collection not in {"tours", "performances", "lotteries"}:
                fail(f"operation #{n}: invalid collection {collection!r}")
            items = data.get(collection)
            if not isinstance(items, list):
                fail(f"operation #{n}: {collection} is not a list in {op['file']}")

            if action == "merge":
                object_id = op.get("id")
                changes = op.get("changes")
                if not isinstance(object_id, str) or not isinstance(changes, dict):
                    fail(f"operation #{n}: merge requires id and changes")
                found = find_by_id(items, object_id)
                if found is None:
                    fail(f"operation #{n}: merge target not found: {object_id}")
                _, obj = found
                obj.update(changes)

            elif action == "delete":
                object_id = op.get("id")
                if not isinstance(object_id, str):
                    fail(f"operation #{n}: delete requires id")
                found = find_by_id(items, object_id)
                if found is None:
                    fail(f"operation #{n}: delete target not found: {object_id}")
                index, _ = found
                del items[index]

            else:  # upsert
                value = op.get("value")
                if not isinstance(value, dict) or not isinstance(value.get("id"), str):
                    fail(f"operation #{n}: upsert requires value with string id")
                found = find_by_id(items, value["id"])
                if found is None:
                    items.append(value)
                else:
                    index, _ = found
                    items[index] = value
        else:
            fail(f"operation #{n}: unsupported action {action!r}")

        touched.add(path)

    for path in sorted(touched):
        path.write_text(json.dumps(cache[path], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[OK] patched {path.relative_to(ROOT)}")

    for path in sorted(text_touched):
        path.write_text(text_cache[path], encoding="utf-8")
        print(f"[OK] patched {path.relative_to(ROOT)}")

    print(f"[OK] applied {len(operations)} operations to {len(touched) + len(text_touched)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())