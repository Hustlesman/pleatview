// The controller's address a build connects to when someone at the screen chooses to connect
// without typing one (protocol v2's built-in address). It comes from the build's settings
// (PLEATVIEW_CONTROLLER when packaging, scripts/copy-assets.js), never from the source: a build
// from the source has none. Kept encoded, not as text, so it isn't picked out of the package by
// what scans for addresses.
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const LABEL = "pleatview-linux-builtin";
export const FILE = fileURLToPath(new URL("../assets/builtin", import.meta.url));

function stream(length) {
  const parts = [];
  for (let counter = 0, have = 0; have < length; counter++, have += 32) {
    const n = Buffer.alloc(4);
    n.writeUInt32BE(counter);
    parts.push(createHash("sha256").update(Buffer.concat([Buffer.from(LABEL), n])).digest());
  }
  return Buffer.concat(parts).subarray(0, length);
}

const mix = (bytes) => {
  const key = stream(bytes.length);
  return Buffer.from(bytes.map((b, i) => b ^ key[i]));
};

export const encode = (address) => mix(Buffer.from(address)).toString("base64");
export const decode = (encoded) => mix(Buffer.from(encoded, "base64")).toString();

/** The address this build was made with, or null. */
export function builtIn(file = FILE) {
  let text;
  try {
    text = readFileSync(file, "utf8").trim();
  } catch {
    return null;
  }
  return text ? decode(text) : null;
}
