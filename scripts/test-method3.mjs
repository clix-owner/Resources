import fs from "node:fs";
import vm from "node:vm";

const html = fs.readFileSync(new URL("../public/index.html", import.meta.url), "utf8");
const source = html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, { id, value: "", textContent: "", innerHTML: "", className: "", disabled: false, dataset: {}, addEventListener(type, callback) { this[`on${type}`] = callback; } });
  return elements.get(id);
};
const document = {
  body: { className: "" },
  getElementById: element,
  querySelectorAll: () => []
};
vm.runInNewContext(source, { document, fetch: async () => { throw new Error("Unexpected fetch"); }, console });

const special = { id: "SPECIAL", season_id: "G65VCD19G", episode: "1", episode_number: 1, sequence_number: 1, title: "Hoshina's Day Off", versions: [{ original: true, guid: "SPECIAL-JP" }] };
const regular = Array.from({ length: 11 }, (_, index) => ({
  id: `DUB-${index + 13}`,
  season_id: "G65VCD19G",
  episode: String(index + 13),
  episode_number: index + 13,
  sequence_number: index + 2,
  title: `Episode ${index + 13}`,
  versions: [{ original: true, guid: `JP-${index + 13}` }]
}));
element("bulkStart").value = "1";
element("bulkEnd").value = "11";
element("bulkEpisodesJson").value = JSON.stringify({ data: [special, ...regular] });
element("extractMediaIds").onclick();

const extracted = element("bulkMediaIds").value.split("\n");
if (extracted.length !== 11 || extracted[0] !== "JP-13" || extracted[10] !== "JP-23" || extracted.includes("SPECIAL-JP")) {
  throw new Error(`Incorrect extraction: ${JSON.stringify(extracted)}`);
}
if (!element("bulkStatus").textContent.includes("Specials skipped: 1")) throw new Error("Numbered special was not reported");

const actualSeasonOne = Array.from({ length: 11 }, (_, index) => ({
  id: `ACTUAL-DUB-${index + 1}`,
  season_id: "G6ABC1234",
  episode: String(index + 1),
  episode_number: index + 1,
  sequence_number: index + 1,
  title: index === 0 ? "The Man Who Became a Kaiju" : `Episode ${index + 1}`,
  identifier: `SERIES|SEASON|E${index + 1}`,
  versions: [{ original: true, guid: `ACTUAL-JP-${index + 1}` }]
}));
element("bulkEpisodesJson").value = JSON.stringify({ data: actualSeasonOne });
element("extractMediaIds").onclick();
const actualExtracted = element("bulkMediaIds").value.split("\n");
if (actualExtracted.length !== 11 || actualExtracted[0] !== "ACTUAL-JP-1" || actualExtracted[10] !== "ACTUAL-JP-11") {
  throw new Error(`Actual E1 was incorrectly skipped: ${JSON.stringify(actualExtracted)}`);
}
if (!element("bulkStatus").textContent.includes("Specials skipped: 0")) throw new Error("Actual E1 was incorrectly reported as a special");

console.log("OK: numbered special E1 is skipped, while a contiguous actual E1-E11 season is preserved.");
