// Copies the files the Linux app shares with the Android app into assets/, which packages carry,
// and the controller's address the build is made with, if any (PLEATVIEW_CONTROLLER), encoded.
import { copyFileSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { FILE, encode } from "../src/builtin.js";
import { address } from "../src/connect.js";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const android = join(root, "..", "android", "app", "src", "main", "assets");
mkdirSync(join(root, "assets"), { recursive: true });
for (const name of ["pleatview.js", "setup.html", "find.js"]) copyFileSync(join(android, name), join(root, "assets", name));
const controller = process.env.PLEATVIEW_CONTROLLER?.trim();
if (controller) writeFileSync(FILE, encode(address(controller)) + "\n");
else rmSync(FILE, { force: true }); // a build without one has none
