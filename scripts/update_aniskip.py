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

ANILIST_URL = "https://graphql.anilist.co"
ANISKIP_URL = "https://api.aniskip.com/v2/skip-times"
USER_AGENT = "aniskip-daily-updater/1.0 (+GitHub Actions)"


def request_json(url: str, *, payload: dict[str, Any] | None = None, attempts: int = 6) -> Any:
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body else "GET")
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


ANILIST_QUERY = """
query ($page: Int!, $status: MediaStatus) {
  Page(page: $page, perPage: 50) {
    pageInfo { hasNextPage }
    media(type: ANIME, status: $status, sort: ID_DESC) {
      id idMal format status episodes
      title { romaji english }
      nextAiringEpisode { episode airingAt }
    }
  }
}
"""


def discover_active() -> list[dict[str, Any]]:
    media: list[dict[str, Any]] = []
    page = 1
    while True:
        result = request_json(ANILIST_URL, payload={"query": ANILIST_QUERY, "variables": {"page": page, "status": "RELEASING"}})
        current = result["data"]["Page"]
        media.extend(x for x in current["media"] if x.get("idMal"))
        if not current["pageInfo"]["hasNextPage"]:
            return media
        page += 1
        time.sleep(0.7)


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
    query = urllib.parse.urlencode([
        ("types[]", "op"), ("types[]", "ed"), ("types[]", "mixed-op"),
        ("types[]", "mixed-ed"), ("episodeLength", "0")
    ])
    url = f"{ANISKIP_URL}/{mal_id}/{episode}?{query}"
    async with semaphore:
        try:
            data = await asyncio.to_thread(request_json, url)
            segment = normalize_segment(data.get("results") or []) if data.get("found") else {}
            return mal_id, episode, segment or None, None
        except Exception as exc:  # keep a transient outage from corrupting the database
            return mal_id, episode, None, f"{type(exc).__name__}: {exc}"


def stable_shard(mal_id: int, episode: int, shards: int) -> int:
    digest = hashlib.blake2s(f"{mal_id}:{episode}".encode(), digest_size=4).digest()
    return int.from_bytes(digest, "big") % shards


def build_jobs(db: dict[str, Any], active: list[dict[str, Any]], full: bool) -> tuple[set[tuple[int, int]], dict[int, dict[str, Any]]]:
    active_by_mal = {int(x["idMal"]): x for x in active}
    jobs: set[tuple[int, int]] = set()

    # Always refresh all already-aired episodes of currently releasing shows.
    for mal_id, item in active_by_mal.items():
        next_ep = (item.get("nextAiringEpisode") or {}).get("episode")
        aired = max(0, int(next_ep) - 1) if next_ep else 0
        existing = db.get(str(item["id"]), {}).get("episodes", {})
        max_known = max((int(x) for x in existing), default=0)
        upper = max(aired, max_known)
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


async def update(path: Path, concurrency: int, full: bool) -> int:
    original = path.read_bytes()
    db = json.loads(original)
    if not isinstance(db, dict) or len(db) < 100:
        raise RuntimeError("Refusing to update: input database is unexpectedly small or invalid")

    active = discover_active()
    if not active:
        raise RuntimeError("AniList returned no releasing anime; refusing to continue")
    jobs, active_by_mal = build_jobs(db, active, full)
    print(f"Active anime: {len(active)}; AniSkip checks: {len(jobs)}")

    semaphore = asyncio.Semaphore(concurrency)
    tasks = [fetch_skip(mal, episode, semaphore) for mal, episode in sorted(jobs)]
    results = await asyncio.gather(*tasks)
    errors = [error for _, _, _, error in results if error]
    if results and len(errors) / len(results) > 0.20:
        raise RuntimeError(f"AniSkip failure rate too high ({len(errors)}/{len(results)}); no file written")

    anilist_for_mal = {int(v.get("malId")): k for k, v in db.items() if isinstance(v, dict) and isinstance(v.get("malId"), int)}
    changed = 0
    for mal_id, episode, segment, error in results:
        if error or not segment:
            continue
        anilist_id = anilist_for_mal.get(mal_id)
        meta = active_by_mal.get(mal_id)
        if anilist_id is None and meta:
            anilist_id = str(meta["id"])
            title = meta.get("title") or {}
            db[anilist_id] = {
                "anilistId": int(anilist_id), "malId": mal_id,
                "title": title.get("english") or title.get("romaji") or f"MAL {mal_id}",
                "format": meta.get("format"), "totalEpisodes": meta.get("episodes"), "episodes": {}
            }
            anilist_for_mal[mal_id] = anilist_id
        if anilist_id is None:
            continue
        episodes = db[anilist_id].setdefault("episodes", {})
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
    print(f"Changed episode records: {changed}; request errors: {len(errors)}")
    return changed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path, default=Path("aniskip_data.json"))
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--full", action="store_true", help="Check every known episode (manual runs only)")
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 20:
        parser.error("--concurrency must be between 1 and 20")
    asyncio.run(update(args.file, args.concurrency, args.full))


if __name__ == "__main__":
    main()
