// Settings on disk: one JSON file per kind, in $XDG_CONFIG_HOME/pleatview (or
// $PLEATVIEW_HOME), readable only by this user. Writes replace the file atomically.
import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export function configDir(env = process.env) {
  return env.PLEATVIEW_HOME || join(env.XDG_CONFIG_HOME || join(homedir(), ".config"), "pleatview");
}

export class Store {
  constructor(dir = configDir()) {
    this.dir = dir;
    mkdirSync(dir, { recursive: true, mode: 0o700 });
  }

  read(name) {
    try {
      return readFileSync(join(this.dir, name), "utf8");
    } catch {
      return null;
    }
  }

  readJson(name, fallback = {}) {
    try {
      return JSON.parse(this.read(name) ?? "null") ?? fallback;
    } catch {
      return fallback;
    }
  }

  write(name, text) {
    const path = join(this.dir, name);
    const temporary = `${path}.${process.pid}.tmp`;
    writeFileSync(temporary, text, { mode: 0o600 });
    renameSync(temporary, path);
  }

  writeJson(name, value) {
    this.write(name, JSON.stringify(value, null, 2));
  }
}
