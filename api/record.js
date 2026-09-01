const GH_API = "https://api.github.com";

function env(name, fallback = "") {
  const value = process.env[name] || fallback;
  if (!value) throw new Error(`Missing environment variable: ${name}`);
  return value;
}

function githubHeaders() {
  return {
    Authorization: `Bearer ${env("GITHUB_TOKEN")}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "Content-Type": "application/json"
  };
}

async function gh(path, options = {}) {
  const response = await fetch(`${GH_API}${path}`, {
    ...options,
    headers: { ...githubHeaders(), ...(options.headers || {}) }
  });
  const text = await response.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = { message: text }; }
  if (!response.ok) {
    const error = new Error(body?.message || `GitHub API returned ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return body;
}

function repoPath(path) {
  const owner = encodeURIComponent(env("GITHUB_OWNER"));
  const repo = encodeURIComponent(env("GITHUB_REPO"));
  return `/repos/${owner}/${repo}${path}`;
}

async function loadDatabase() {
  const branch = env("GITHUB_BRANCH", "main");
  const dataPath = env("GITHUB_DATA_PATH", "aniskip_data.json");
  const ref = await gh(repoPath(`/git/ref/heads/${encodeURIComponent(branch)}`));
  const commit = await gh(repoPath(`/git/commits/${ref.object.sha}`));
  const tree = await gh(repoPath(`/git/trees/${commit.tree.sha}?recursive=1`));
  const entry = tree.tree?.find((item) => item.type === "blob" && item.path === dataPath);
  if (!entry) throw Object.assign(new Error(`Database file not found: ${dataPath}`), { status: 404 });
  const blob = await gh(repoPath(`/git/blobs/${entry.sha}`));
  const json = Buffer.from(blob.content.replace(/\n/g, ""), blob.encoding || "base64").toString("utf8");
  return { database: JSON.parse(json), headSha: ref.object.sha, treeSha: commit.tree.sha, dataPath };
}

function findAnime(database, malId) {
  return Object.entries(database).find(([, anime]) => Number(anime?.malId) === malId) || null;
}

async function animeFromMal(malId) {
  const query = `query ($idMal: Int) { Media(idMal: $idMal, type: ANIME) { id idMal format episodes title { romaji english } } }`;
  const response = await fetch("https://graphql.anilist.co", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ query, variables: { idMal: malId } })
  });
  if (!response.ok) throw new Error(`AniList lookup failed (${response.status})`);
  const media = (await response.json())?.data?.Media;
  if (!media) throw Object.assign(new Error(`No anime found for MAL ID ${malId}`), { status: 404 });
  return {
    key: String(media.id),
    value: {
      anilistId: media.id,
      malId,
      title: media.title?.english || media.title?.romaji || `MAL ${malId}`,
      format: media.format || "UNKNOWN",
      totalEpisodes: media.episodes || 0,
      episodes: {}
    }
  };
}

function range(value, label) {
  if (value == null) return null;
  const start = Number(value.start);
  const end = Number(value.end);
  if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end <= start) {
    throw Object.assign(new Error(`${label} must have valid start/end seconds`), { status: 400 });
  }
  return { start, end };
}

async function writeDatabase(database, baseTreeSha, parentSha, dataPath, message) {
  const content = `${JSON.stringify(database, null, 2)}\n`;
  const blob = await gh(repoPath("/git/blobs"), {
    method: "POST",
    body: JSON.stringify({ content: Buffer.from(content).toString("base64"), encoding: "base64" })
  });
  const tree = await gh(repoPath("/git/trees"), {
    method: "POST",
    body: JSON.stringify({
      base_tree: baseTreeSha,
      tree: [{ path: dataPath, mode: "100644", type: "blob", sha: blob.sha }]
    })
  });
  const commit = await gh(repoPath("/git/commits"), {
    method: "POST",
    body: JSON.stringify({ message, tree: tree.sha, parents: [parentSha] })
  });
  await gh(repoPath(`/git/refs/heads/${encodeURIComponent(env("GITHUB_BRANCH", "main"))}`), {
    method: "PATCH",
    body: JSON.stringify({ sha: commit.sha, force: false })
  });
  return commit.sha;
}

function send(res, status, payload) {
  res.status(status).setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.end(JSON.stringify(payload));
}

async function fetchCrunchyroll(mediaId) {
  const id = String(mediaId || "").trim().toUpperCase();
  if (!/^[A-Z0-9]{6,20}$/.test(id)) {
    throw Object.assign(new Error("Invalid Crunchyroll media ID"), { status: 400 });
  }
  const response = await fetch(`https://static.crunchyroll.com/skip-events/production/${encodeURIComponent(id)}.json`, {
    headers: { Accept: "application/json" },
    signal: AbortSignal.timeout(15000)
  });
  if (!response.ok) {
    throw Object.assign(new Error(`Crunchyroll skip data unavailable for ${id} (${response.status})`), { status: 404 });
  }
  let data;
  try { data = JSON.parse(await response.text()); }
  catch { throw Object.assign(new Error("Crunchyroll returned a non-JSON response"), { status: 502 }); }
  const op = data.intro ? range(data.intro, "Crunchyroll intro") : null;
  const ed = data.credits ? range(data.credits, "Crunchyroll credits") : null;
  if (!op && !ed) throw Object.assign(new Error(`No intro or credits timestamps found for ${id}`), { status: 404 });
  return { requestedMediaId: id, mediaId: data.mediaId || id, op, ed, lastUpdated: data.lastUpdated || null };
}

async function fetchAllCrunchyroll(mediaIds, limit = 6) {
  const results = new Array(mediaIds.length);
  let cursor = 0;
  async function worker() {
    while (cursor < mediaIds.length) {
      const index = cursor;
      cursor += 1;
      try {
        results[index] = { ok: true, value: await fetchCrunchyroll(mediaIds[index]) };
      } catch (error) {
        // A 404 means this media item has no usable skip-event data. It is a
        // valid episode to omit, not a reason to discard timestamps fetched
        // for the rest of an ordered range. Auth, rate-limit and network
        // errors still stop the batch because their outcome is uncertain.
        if (error.status === 404) {
          results[index] = { ok: false, mediaId: mediaIds[index], error: error.message };
        } else {
          throw error;
        }
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, mediaIds.length) }, worker));
  return results;
}

export default async function handler(req, res) {
  try {
    if (req.method === "GET") {
      if (req.query.mode === "crunchyroll") {
        const result = await fetchCrunchyroll(req.query.mediaId);
        return send(res, 200, { ok: true, ...result });
      }
      const malId = Number(req.query.malId);
      if (!Number.isInteger(malId) || malId <= 0) return send(res, 400, { ok: false, error: "A valid malId is required" });
      const { database } = await loadDatabase();
      const found = findAnime(database, malId);
      if (req.query.mode === "missing") {
        if (!found) return send(res, 404, { ok: false, error: `MAL ID ${malId} is not in the database` });
        const anime = found[1];
        const storedEpisodes = Object.keys(anime.episodes || {}).map(Number).filter(Number.isInteger);
        const maxStored = storedEpisodes.length ? Math.max(...storedEpisodes) : 0;
        const requestedThrough = Number(req.query.through);
        const through = Number.isInteger(requestedThrough) && requestedThrough > 0
          ? requestedThrough
          : (Number(anime.totalEpisodes) || maxStored);
        const items = [];
        for (let number = 1; number <= through; number += 1) {
          const record = anime.episodes?.[String(number)] || null;
          const missing = [];
          if (!record?.op) missing.push("op");
          if (!record?.ed) missing.push("ed");
          if (missing.length) items.push({ episode: number, missing, record });
        }
        return send(res, 200, {
          ok: true,
          anime: anime.title,
          malId,
          through,
          totalEpisodes: anime.totalEpisodes ?? null,
          maxStored,
          count: items.length,
          items
        });
      }
      const episode = Number(req.query.episode);
      if (!Number.isInteger(episode) || episode <= 0) return send(res, 400, { ok: false, error: "A valid episode is required" });
      const record = found?.[1]?.episodes?.[String(episode)] || null;
      return send(res, 200, { ok: true, exists: Boolean(record), anime: found?.[1]?.title || null, record });
    }

    if (req.method !== "POST") return send(res, 405, { ok: false, error: "Method not allowed" });
    const providedKey = req.headers["x-admin-key"] || req.body?.adminKey;
    if (!providedKey || providedKey !== env("ADMIN_KEY")) return send(res, 401, { ok: false, error: "Invalid admin key" });

    const malId = Number(req.body?.malId);
    if (req.body?.mode === "bulk") {
      const startEpisode = Number(req.body?.startEpisode);
      const endEpisode = Number(req.body?.endEpisode);
      const mediaIds = Array.isArray(req.body?.mediaIds) ? req.body.mediaIds : [];
      if (!Number.isInteger(malId) || malId <= 0 || !Number.isInteger(startEpisode) || !Number.isInteger(endEpisode) || startEpisode <= 0 || endEpisode < startEpisode) {
        return send(res, 400, { ok: false, error: "MAL ID and a valid episode range are required" });
      }
      const expected = endEpisode - startEpisode + 1;
      if (mediaIds.length !== expected) {
        return send(res, 400, { ok: false, error: `Episode range needs exactly ${expected} media IDs; received ${mediaIds.length}` });
      }
      if (expected > 200) return send(res, 400, { ok: false, error: "Use batches of 200 episodes or fewer" });

      // Fetch every item before changing GitHub. Timestamp-less (404) items
      // are skipped individually; uncertain API failures still abort the run.
      const timestamps = await fetchAllCrunchyroll(mediaIds);
      const usable = timestamps.filter((item) => item.ok);
      if (!usable.length) return send(res, 404, { ok: false, error: "None of these media IDs have intro or credits timestamps" });
      for (let attempt = 0; attempt < 2; attempt += 1) {
        const loaded = await loadDatabase();
        let found = findAnime(loaded.database, malId);
        let createdAnime = false;
        if (!found) {
          const created = await animeFromMal(malId);
          if (loaded.database[created.key] && Number(loaded.database[created.key]?.malId) !== malId) {
            throw Object.assign(new Error("AniList ID key collision"), { status: 409 });
          }
          loaded.database[created.key] = created.value;
          found = [created.key, loaded.database[created.key]];
          createdAnime = true;
        }
        const [, anime] = found;
        anime.episodes ||= {};
        const records = [];
        const skipped = [];
        timestamps.forEach((result, index) => {
          const episode = startEpisode + index;
          if (!result.ok) {
            skipped.push({ episode, mediaId: result.mediaId, error: result.error });
            return;
          }
          const item = result.value;
          const previous = anime.episodes[String(episode)] || {};
          const record = { ...previous, ...(item.op ? { op: item.op } : {}), ...(item.ed ? { ed: item.ed } : {}) };
          anime.episodes[String(episode)] = record;
          records.push({ episode, requestedMediaId: item.requestedMediaId, mediaId: item.mediaId, record, complete: Boolean(record.op && record.ed) });
        });
        anime.totalEpisodes = Math.max(Number(anime.totalEpisodes) || 0, endEpisode);
        try {
          const sha = await writeDatabase(
            loaded.database,
            loaded.treeSha,
            loaded.headSha,
            loaded.dataPath,
            `Update MAL ${malId} episodes ${startEpisode}-${endEpisode} Crunchyroll skip times`
          );
          return send(res, 200, { ok: true, action: "bulk-updated", createdAnime, anime: anime.title, malId, startEpisode, endEpisode, count: records.length, records, skipped, commit: sha });
        } catch (error) {
          if (error.status !== 422 || attempt === 1) throw error;
        }
      }
    }
    const episode = Number(req.body?.episode);
    if (!Number.isInteger(malId) || malId <= 0 || !Number.isInteger(episode) || episode <= 0) {
      return send(res, 400, { ok: false, error: "MAL ID and episode must be positive integers" });
    }
    let op = range(req.body?.op, "Opening");
    let ed = range(req.body?.ed, "Credits");
    let crunchyroll = null;
    if (req.body?.mediaId) {
      crunchyroll = await fetchCrunchyroll(req.body.mediaId);
      op ||= crunchyroll.op;
      ed ||= crunchyroll.ed;
    }
    if (!op && !ed) return send(res, 400, { ok: false, error: "Provide at least one opening or credits range" });

    // Retry once if another submission moves the branch while this one is writing.
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const loaded = await loadDatabase();
      let found = findAnime(loaded.database, malId);
      let createdAnime = false;
      if (!found) {
        const created = await animeFromMal(malId);
        if (loaded.database[created.key] && Number(loaded.database[created.key]?.malId) !== malId) {
          throw Object.assign(new Error("AniList ID key collision"), { status: 409 });
        }
        loaded.database[created.key] = created.value;
        found = [created.key, loaded.database[created.key]];
        createdAnime = true;
      }
      const [, anime] = found;
      const previous = anime.episodes?.[String(episode)] || null;
      anime.episodes ||= {};
      anime.episodes[String(episode)] = {
        ...(previous || {}),
        ...(op ? { op } : {}),
        ...(ed ? { ed } : {})
      };
      anime.totalEpisodes = Math.max(Number(anime.totalEpisodes) || 0, episode);

      try {
        const action = previous ? "Update" : "Add";
        const sha = await writeDatabase(
          loaded.database,
          loaded.treeSha,
          loaded.headSha,
          loaded.dataPath,
          `${action} MAL ${malId} episode ${episode} skip times`
        );
        return send(res, 200, {
          ok: true,
          action: previous ? "updated" : "created",
          createdAnime,
          anime: anime.title,
          malId,
          episode,
          record: anime.episodes[String(episode)],
          complete: Boolean(anime.episodes[String(episode)].op && anime.episodes[String(episode)].ed),
          crunchyroll: crunchyroll ? { requestedMediaId: crunchyroll.requestedMediaId, mediaId: crunchyroll.mediaId } : null,
          commit: sha
        });
      } catch (error) {
        if (error.status !== 422 || attempt === 1) throw error;
      }
    }
  } catch (error) {
    console.error(error);
    return send(res, error.status || 500, { ok: false, error: error.message || "Unexpected server error" });
  }
}
