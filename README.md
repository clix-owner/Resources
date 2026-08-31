# Anime Skip DB Editor for Vercel

A small protected web UI and serverless API that updates an existing AniSkip-style JSON database in GitHub.

## Behaviour

- Method 1 directly adds or updates one MAL ID + episode record.
- Method 2 loads a missing/partial episode queue for a MAL ID.
- Looks up records by `malId` and episode number.
- Lists episodes with no record, only `op`, or only `ed`, with an optional upper episode limit for ongoing anime.
- Provides a one-by-one queue; completed records disappear immediately after a successful submission.
- Updates supplied ranges in an existing episode while preserving its other timestamp range.
- Creates a missing episode record.
- If the MAL anime itself is missing, AniList is queried by MAL ID and the normal top-level AniList ID record is created automatically.
- Accepts opening and credits start/end timestamps manually in seconds, merging supplied ranges while preserving an existing counterpart.
- Uses GitHub's Git Data API, suitable for the included JSON file which is larger than the simple Contents API read limit.
- Creates a normal Git commit for every successful submission and retries once on a concurrent branch update.

## GitHub setup

1. Create a repository and place `data/aniskip_data.json` in it (or move it to another path).
2. Create a fine-grained GitHub token scoped only to that repository with **Contents: Read and write**.
3. Import this project into Vercel.
4. Add the environment variables shown in `.env.example`.
5. Set `GITHUB_DATA_PATH` to the exact repository-relative JSON path, for example `data/aniskip_data.json`.
6. Redeploy, open the Vercel URL, and submit through the UI.

Never put `GITHUB_TOKEN` or `ADMIN_KEY` into frontend code or commit a real `.env` file.

## Local validation

```bash
npm install
npm run check
npx vercel dev
```

The included `data/aniskip_data.json` is an exact copy of the supplied starting database.
