// Announces the control API as `_pleatview._tcp`, as the Android app does, so Home
// Assistant discovers the screen. TXT records: id, name, api, version.
import ciao from "@homebridge/ciao";

export class Advertiser {
  constructor() {
    this.responder = ciao.getResponder();
    this.service = null;
  }

  /** Starts announcing, or updates the announcement after the name changed. */
  async announce({ id, name, port, api, version }) {
    const txt = { id, name, api: String(api), version };
    if (this.service && this.name === name) return this.service.updateTxt(txt);
    await this.stop();
    this.name = name;
    // Named like the Android app's service; ciao renames it if another one has the name.
    this.service = this.responder.createService({ name: name.slice(0, 63), type: "pleatview", protocol: "tcp", port, txt });
    await this.service.advertise();
  }

  async stop() {
    if (!this.service) return;
    const service = this.service;
    this.service = null;
    await service.end().catch(() => {});
  }

  async shutdown() {
    await this.stop();
    await this.responder.shutdown().catch(() => {});
  }
}
