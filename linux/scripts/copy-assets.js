// Copies the files the Linux app shares with the Android app into assets/, which packages carry.
import { copyFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const android = join(root, "..", "android", "app", "src", "main", "assets");
mkdirSync(join(root, "assets"), { recursive: true });
for (const name of ["pleatview.js", "setup.html"]) copyFileSync(join(android, name), join(root, "assets", name));
