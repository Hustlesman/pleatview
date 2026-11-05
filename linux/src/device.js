// What this computer is and where it can be reached, for /api/info, mDNS and the setup screen.
import { randomBytes } from "node:crypto";
import { readFileSync } from "node:fs";
import { hostname, networkInterfaces } from "node:os";

const read = (path) => {
  try {
    return readFileSync(path, "utf8").replace(/\0/g, "").trim();
  } catch {
    return "";
  }
};

/** Manufacturer and model: a Raspberry Pi's board, or a PC's DMI vendor and product. */
export function hardware() {
  const board = read("/sys/firmware/devicetree/base/model");
  if (board) return { manufacturer: board.startsWith("Raspberry Pi") ? "Raspberry Pi" : board.split(" ")[0], model: board };
  const vendor = read("/sys/class/dmi/id/sys_vendor");
  const product = read("/sys/class/dmi/id/product_name");
  const generic = (value) => !value || /to be filled|default string|system product name|not specified/i.test(value);
  return {
    manufacturer: generic(vendor) ? "Linux" : vendor,
    model: generic(product) ? `${process.arch} computer` : product,
  };
}

/** The screen's id: random, made once and kept. */
export function deviceId(store) {
  const stored = store.readJson("device.json", {});
  if (typeof stored.id === "string" && /^[0-9a-f]{32}$/.test(stored.id)) return stored.id;
  const id = randomBytes(16).toString("hex");
  store.writeJson("device.json", { ...stored, id });
  return id;
}

/** The name shown in Home Assistant until it's changed there: the computer's host name. */
export const defaultName = () => hostname().split(".")[0] || "PleatView";

/**
 * The IPv4 address Home Assistant and phones on the LAN can reach: a private address on a
 * wired or wireless interface, before containers' and VPNs' bridges.
 */
export function lanAddress() {
  const candidates = [];
  for (const [name, addresses] of Object.entries(networkInterfaces())) {
    for (const a of addresses ?? []) {
      if (a.family !== "IPv4" || a.internal) continue;
      const virtual = /^(docker|br-|veth|virbr|lxc|tun|tap|wg|zt|tailscale)/.test(name);
      const lan = /^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(a.address);
      candidates.push({ address: a.address, rank: (virtual ? 2 : 0) + (lan ? 0 : 1) });
    }
  }
  candidates.sort((a, b) => a.rank - b.rank);
  return candidates[0]?.address ?? "127.0.0.1";
}
