import fs from "node:fs";
import vm from "node:vm";

const db = JSON.parse(fs.readFileSync(new URL("../data/aniskip_data.json", import.meta.url), "utf8"));
if (!db || typeof db !== "object" || Array.isArray(db)) throw new Error("Database root must be an object");
const source = fs.readFileSync(new URL("../api/record.js", import.meta.url), "utf8")
  .replace(/export default async function handler/, "async function handler");
new vm.Script(source, { filename: "api/record.js" });
console.log(`OK: ${Object.keys(db).length} anime records; API syntax valid.`);
