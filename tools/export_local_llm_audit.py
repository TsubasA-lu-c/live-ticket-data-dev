#!/usr/bin/env python3
"""事実抽出runをChatGPT監査用ZIPへまとめる。標準ライブラリのみ。"""

import argparse
import json
import shutil
import tempfile
import zipfile
from pathlib import Path


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="local LLM事実抽出runの監査ZIP生成")
    ap.add_argument("--run-dir", type=Path, required=True)
    ap.add_argument("--queue", type=Path, default=Path("cache/ai_queue.json"))
    ap.add_argument("--output", type=Path, default=None)
    ap.add_argument("--artist-dir", type=Path, default=Path("data/artist"))
    ap.add_argument("--relations", type=Path, default=Path("config/artist_relations.json"))
    ap.add_argument(
        "--targets",
        type=Path,
        default=Path("config/collect_targets.json"),
    )
    args = ap.parse_args()

    run_dir = args.run_dir.resolve()
    if not run_dir.exists():
        raise SystemExit(f"run-dirがありません: {run_dir}")

    queue = load_json(args.queue)
    queue_by_id = {
        x.get("artistId"): x
        for x in queue.get("items", [])
        if x.get("artistId")
    }

    input_data = load_json(run_dir / "input.json")
    artist_ids = [
        x.get("artistId")
        for x in input_data.get("items", [])
        if x.get("artistId")
    ]

    output = args.output
    if output is None:
        output = Path("local_llm/audits") / f"audit-{run_dir.name}.zip"
    output = output.resolve()

    with tempfile.TemporaryDirectory(prefix="local-llm-audit-") as td:
        stage = Path(td) / "audit"
        stage.mkdir(parents=True)

        for name in ("input.json", "facts.json", "classification.json", "rejected.json", "errors.json", "report.json"):
            src = run_dir / name
            if src.exists():
                shutil.copy2(src, stage / name)

        selected_queue = {
            "generatedAt": queue.get("generatedAt"),
            "instructions": queue.get("instructions"),
            "items": [
                queue_by_id[aid]
                for aid in artist_ids
                if aid in queue_by_id
            ],
        }
        (stage / "ai_queue_selected.json").write_text(
            json.dumps(selected_queue, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        if args.targets.exists():
            shutil.copy2(args.targets, stage / "collect_targets.json")
        if args.relations.exists():
            shutil.copy2(args.relations, stage / "artist_relations.json")

        existing = stage / "existing_data"
        existing.mkdir()
        for aid in artist_ids:
            src = args.artist_dir / f"{aid}.json"
            if src.exists():
                shutil.copy2(src, existing / src.name)

        (stage / "artists.txt").write_text(
            "\n".join(artist_ids) + "\n",
            encoding="utf-8",
        )

        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            output.unlink()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(stage.rglob("*")):
                if p.is_file():
                    z.write(p, p.relative_to(stage.parent))

    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
