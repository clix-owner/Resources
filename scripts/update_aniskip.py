#!/usr/bin/env python3
"""Incrementally update an AniSkip JSON database without third-party packages."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAL_API_URL = "https://api.myanimelist.net/v2"
ANISKIP_URL = "https://api.aniskip.com/v2/skip-times"
USER_AGENT = "aniskip-daily-updater/1.0 (+GitHub Actions)"


def request_json(url: str, *, extra_headers: dict[str, str] | None = None, attempts: int = 6) -> Any:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    headers.update(extra_headers or {})
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=45) as response:
                return json.load(response)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            code = getattr(exc, "code", 0)
            if code and code not in (429, 500, 502, 503, 504):
                raise
            if attempt == attempts - 1:
                raise
            retry_after = getattr(exc, "headers", {}).get("Retry-After") if getattr(exc, "headers", None) else None
            delay = float(retry_after) if retry_after and retry_after.isdigit() else min(60, 2 ** attempt + random.random())
            time.sleep(delay)
    raise RuntimeError("unreachable")


def mal_get(path_or_url: str, client_id: str) -> Any:
    url = path_or_url if path_or_url.startswith("https://") else f"{MAL_API_URL}{path_or_url}"
    return request_json(url, extra_headers={"X-MAL-CLIENT-ID": client_id})


def discover_active(client_id: str) -> list[dict[str, Any]]:
    """Get currently airing titles from official MAL API v2 only."""
    fields = "id,title,media_type,status,num_episodes,start_date,end_date,broadcast"
    url = f"/anime/ranking?ranking_type=airing&limit=500&fields={urllib.parse.quote(fields)}"
    found: dict[int, dict[str, Any]] = {}
    while url:
        page = mal_get(url, client_id)
        for row in page.get("data", []):
            node = row.get("node") or {}
            if node.get("id") and node.get("status") == "currently_airing":
                found[int(node["id"])] = node
        url = (page.get("paging") or {}).get("next")
        if url:
            time.sleep(0.5)
    return list(found.values())


def normalize_segment(results: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    chosen: dict[str, dict[str, float]] = {}
    priority = {"op": ("op", 2), "mixed-op": ("op", 1), "ed": ("ed", 2), "mixed-ed": ("ed", 1)}
    scores: dict[str, int] = {}
    for item in results:
        mapped = priority.get(item.get("skipType"))
        interval = item.get("interval") or {}
        if not mapped or not isinstance(interval.get("startTime"), (int, float)) or not isinstance(interval.get("endTime"), (int, float)):
            continue
        key, score = mapped
        if score <= scores.get(key, -1):
            continue
        start = round(float(interval["startTime"]), 3)
        end = round(float(interval["endTime"]), 3)
        if start < 0 or end <= start:
            continue
        chosen[key] = {"start": start, "end": end}
        scores[key] = score
    return chosen


async def fetch_skip(mal_id: int, episode: int, semaphore: asyncio.Semaphore) -> tuple[int, int, dict[str, Any] | None, str | None]:
    # episodeLength is optional. Do NOT send 0: current AniSkip validation can
    # reject it, which turns every lookup into an HTTP 4xx failure.
    query = urllib.parse.urlencode([
        ("types[]", "op"), ("types[]", "ed"), ("types[]", "mixed-op"),
        ("types[]", "mixed-ed")
    ])
    url = f"{ANISKIP_URL}/{mal_id}/{episode}?{query}"
    async with semaphore:
        try:
            data = await asyncio.to_thread(request_json, url)
            segment = normalize_segment(data.get("results") or []) if data.get("found") else {}
            return mal_id, episode, segment or None, None
        except Exception as exc:  # keep a transient outage from corrupting the database
            return mal_id, episode, None, f"{type(exc).__name__}: {exc}"


def stable_shard(mal_id: int, episode: int | str, shards: int) -> int:
    digest = hashlib.blake2s(f"{mal_id}:{episode}".encode(), digest_size=4).digest()
    return int.from_bytes(digest, "big") % shards


def build_jobs(db: dict[str, Any], active: list[dict[str, Any]], full: bool) -> tuple[set[tuple[int, int]], dict[int, dict[str, Any]]]:
    active_by_mal = {int(x["id"]): x for x in active}
    jobs: set[tuple[int, int]] = set()

    # Always refresh all already-aired episodes of currently releasing shows.
    for mal_id, item in active_by_mal.items():
        total = item.get("num_episodes")
        existing = db.get(str(mal_id), {}).get("episodes", {})
        max_known = max((int(float(x)) for x in existing), default=0)
        # MAL does not expose a next-episode number. Probe the next two episode
        # numbers for airing shows; only found AniSkip records are stored.
        upper = max(max_known + 2, int(total or 0))
        if max_known == 0 and not total:
            upper = 4
        for episode in range(1, upper + 1):
            if episode not in range(max(1, upper - 3), upper + 1) and str(episode) in existing:
                continue
            jobs.add((mal_id, episode))

    # Slowly revisit the full catalogue, including gaps, so late community
    # submissions are eventually merged. Each pair is checked once per 90 days.
    shard = datetime.now(timezone.utc).date().toordinal() % 90
    for entry in db.values():
        mal_id = entry.get("malId")
        total = entry.get("totalEpisodes")
        if not isinstance(mal_id, int) or not isinstance(total, int) or total <= 0 or total > 2000:
            continue
        for episode in range(1, total + 1):
            if full or stable_shard(mal_id, episode, 90) == shard:
                jobs.add((mal_id, episode))
    return jobs, active_by_mal


def migrate_to_mal_keys(db: dict[str, Any]) -> tuple[dict[str, Any], int]:
    migrated: dict[str, Any] = {}
    count = 0
    for old_key, entry in db.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("malId"), int):
            continue
        mal_key = str(entry["malId"])
        clean = dict(entry)
        if "anilistId" in clean:
            clean.pop("anilistId")
            count += 1
        if old_key != mal_key:
            count += 1
        if mal_key in migrated:
            migrated[mal_key].setdefault("episodes", {}).update(clean.get("episodes") or {})
        else:
            migrated[mal_key] = clean
    if len(migrated) < 100:
        raise RuntimeError("MAL-key migration produced an unexpectedly small database")
    return migrated, count


async def update(path: Path, concurrency: int, full: bool, shard_index: int, shard_count: int) -> int:
    original = path.read_bytes()
    db = json.loads(original)
    if not isinstance(db, dict) or len(db) < 100:
        raise RuntimeError("Refusing to update: input database is unexpectedly small or invalid")

    db, migration_changes = migrate_to_mal_keys(db)
    client_id = os.environ.get("MAL_CLIENT_ID", "").strip()
    if not client_id:
        raise RuntimeError("MAL_CLIENT_ID is missing. Add it in GitHub Actions repository secrets.")
    active = discover_active(client_id)
    if not active:
        raise RuntimeError("Official MAL API returned no currently airing anime; refusing to continue")
    jobs, active_by_mal = build_jobs(db, active, full)
    jobs = {job for job in jobs if stable_shard(job[0], job[1], shard_count) == shard_index}
    print(
        f"Shard {shard_index + 1}/{shard_count} | MAL currently airing anime: "
        f"{len(active)} | AniSkip checks: {len(jobs)}",
        flush=True,
    )

    # Fail fast before spending hours retrying a broken/blocked endpoint.
    # Any valid JSON response (including found=false) proves the API is reachable.
    if jobs:
        test_mal, test_episode = sorted(jobs)[0]
        test_query = urllib.parse.urlencode([("types[]", "op"), ("types[]", "ed")])
        test_url = f"{ANISKIP_URL}/{test_mal}/{test_episode}?{test_query}"
        try:
            probe = await asyncio.to_thread(request_json, test_url, attempts=2)
            if not isinstance(probe, dict) or "found" not in probe:
                raise RuntimeError(f"unexpected response: {str(probe)[:300]}")
            print(f"AniSkip preflight OK: MAL {test_mal} episode {test_episode}", flush=True)
        except Exception as exc:
            raise RuntimeError(
                f"AniSkip preflight failed; aborting before bulk checks: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    semaphore = asyncio.Semaphore(concurrency)
    tasks = [asyncio.create_task(fetch_skip(mal, episode, semaphore)) for mal, episode in sorted(jobs)]
    results = []
    for completed, task in enumerate(asyncio.as_completed(tasks), 1):
        results.append(await task)
        if completed == 1 or completed % 25 == 0 or completed == len(tasks):
            print(
                f"Shard {shard_index + 1}/{shard_count}: checked {completed}/{len(tasks)} episodes",
                flush=True,
            )
    errors = [error for _, _, _, error in results if error]
    if errors:
        print("AniSkip sample errors:", flush=True)
        for error in errors[:5]:
            print(f"  - {error}", flush=True)
    if results and len(errors) / len(results) > 0.20:
        raise RuntimeError(f"AniSkip failure rate too high ({len(errors)}/{len(results)}); no file written")

    changed = migration_changes
    for mal_id, episode, segment, error in results:
        if error or not segment:
            continue
        meta = active_by_mal.get(mal_id)
        mal_key = str(mal_id)
        if mal_key not in db and meta:
            db[mal_key] = {
                "malId": mal_id, "title": meta.get("title") or f"MAL {mal_id}",
                "format": str(meta.get("media_type") or "unknown").upper(),
                "totalEpisodes": meta.get("num_episodes") or None, "episodes": {}
            }
        if mal_key not in db:
            continue
        if meta and meta.get("num_episodes") and db[mal_key].get("totalEpisodes") != meta["num_episodes"]:
            db[mal_key]["totalEpisodes"] = meta["num_episodes"]
            changed += 1
        episodes = db[mal_key].setdefault("episodes", {})
        if episodes.get(str(episode)) != segment:
            episodes[str(episode)] = segment
            changed += 1

    if changed:
        ordered = dict(sorted(db.items(), key=lambda item: int(item[0])))
        encoded = (json.dumps(ordered, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        json.loads(encoded)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_bytes(encoded)
        os.replace(temp, path)
    print(f"Changed/migrated records: {changed}; request errors: {len(errors)}", flush=True)
    return changed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path, default=Path("aniskip_data.json"))
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--full", action="store_true", help="Check every known episode (manual runs only)")
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 20:
        parser.error("--concurrency must be between 1 and 20")
    if args.shard_count < 1 or not 0 <= args.shard_index < args.shard_count:
        parser.error("shard index must be between 0 and shard-count - 1")
    asyncio.run(update(args.file, args.concurrency, args.full, args.shard_index, args.shard_count))


if __name__ == "__main__":
    main()
