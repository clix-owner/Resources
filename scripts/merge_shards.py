#!/usr/bin/env python3
"""Merge independently updated AniSkip shard files into the canonical JSON."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path


def load_updater(script: Path):
    spec = importlib.util.spec_from_file_location("aniskip_updater", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load updater module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, default=Path("aniskip_data.json"))
    parser.add_argument("--shards-dir", type=Path, default=Path("shard-results"))
    parser.add_argument("--shard-count", type=int, required=True)
    args = parser.parse_args()

    updater = load_updater(Path(__file__).with_name("update_aniskip.py"))
    base, _ = updater.migrate_to_mal_keys(json.loads(args.base.read_text(encoding="utf-8")))

    for shard_index in range(args.shard_count):
        candidates = list(args.shards_dir.glob(f"shard-{shard_index}/**/aniskip_data.json"))
        if len(candidates) != 1:
            raise RuntimeError(f"Expected one result for shard {shard_index}, found {len(candidates)}")
        shard_db, _ = updater.migrate_to_mal_keys(json.loads(candidates[0].read_text(encoding="utf-8")))
        for mal_key, source in shard_db.items():
            mal_id = int(mal_key)
            target = base.setdefault(mal_key, {k: v for k, v in source.items() if k != "episodes"})
            for key, value in source.items():
                if key != "episodes":
                    target[key] = value
            target_episodes = target.setdefault("episodes", {})
            for episode, segment in (source.get("episodes") or {}).items():
                if updater.stable_shard(mal_id, episode, args.shard_count) == shard_index:
                    target_episodes[episode] = segment

    ordered = dict(sorted(base.items(), key=lambda item: int(item[0])))
    args.base.write_text(json.dumps(ordered, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"Merged {args.shard_count} shards into {len(ordered)} MAL titles", flush=True)


if __name__ == "__main__":
    main()
