# AniSkip daily updater

This repository-ready package updates `aniskip_data.json` every day using the
official MyAnimeList API v2 and the official AniSkip v2 API. The JSON is keyed
by MAL ID and does not depend on AniList.

## Setup

1. Copy all files to the root of a GitHub repository.
2. Create a MyAnimeList API application and copy its Client ID.
3. Add it at **Settings → Secrets and variables → Actions → New repository
   secret** with the exact name `MAL_CLIENT_ID`.
4. Push to the repository's default branch.
5. Open **Actions → Update AniSkip database → Run workflow** for the first test.

The workflow has `contents: write` permission, validates the JSON, migrates old
AniList-keyed records to MAL keys, preserves timestamps, prints live progress,
and commits only when data actually changes.

Every run uses eight deterministic parallel shards. Each episode belongs to
exactly one shard, each shard uploads its result, and a final job validates and
merges all results before making one commit. A full scan of 67,535 checks
therefore becomes roughly 8,442 checks per job instead of one giant job.

Daily non-full runs update currently airing shows and one deterministic slice
of the older catalogue. A manual workflow run can enable `full_scan`.

If organization/repository policy blocks the push, enable **Settings → Actions
→ General → Workflow permissions → Read and write permissions**.
