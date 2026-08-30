# AniSkip daily updater

This repository-ready package updates `aniskip_data.json` every day using
AniList metadata and the official AniSkip v2 API.

## Setup

1. Copy all files to the root of a GitHub repository.
2. Push to the repository's default branch.
3. Open **Actions → Update AniSkip database → Run workflow** for the first test.

No API key or repository secret is required. The workflow has `contents: write`
permission, validates the JSON, preserves old data, and commits only when an
episode timestamp actually changes.

Daily runs update currently airing shows and one deterministic slice of the
older catalogue. This catches newly submitted historical timestamps over a
90-day cycle without sending an unsafe number of requests in one run. A manual
workflow run can enable `full_scan`, but it may take much longer.

If organization/repository policy blocks the push, enable **Settings → Actions
→ General → Workflow permissions → Read and write permissions**.
