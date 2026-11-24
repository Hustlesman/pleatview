// The announcement (src/mdns.js): its TXT records name the screen and say what it is (maker and
// model, as /api/info does), for a mark beside it where it's found; long values are cut.
import { test } from "node:test";
import assert from "node:assert/strict";
import { Advertiser } from "../src/mdns.js";

function fake() {
  const made = [];
  const advertiser = Object.create(Advertiser.prototype);
  advertiser.service = null;
  advertiser.responder = {
    createService(options) {
      made.push(options);
      return { advertise: async () => {}, end: async () => {}, updateTxt: async (txt) => made.push({ txt }) };
    },
  };
  return { advertiser, made };
}

test("the TXT records say what the screen is", async () => {
  const { advertiser, made } = fake();
  await advertiser.announce({ id: "abc", name: "Lobby", port: 8765, api: 1, version: "1.1.5", manufacturer: "Raspberry Pi", model: "Raspberry Pi 5 Model B Rev 1.0" });
  assert.deepEqual(made[0].txt, { id: "abc", name: "Lobby", api: "1", version: "1.1.5", manufacturer: "Raspberry Pi", model: "Raspberry Pi 5 Model B Rev 1.0" });
  assert.equal(made[0].type, "pleatview");
});

test("long makers and models are cut; none are empty", async () => {
  const { advertiser, made } = fake();
  await advertiser.announce({ id: "abc", name: "Lobby", port: 8765, api: 1, version: "1.1.5", model: "x".repeat(200) });
  assert.equal(made[0].txt.model.length, 60);
  assert.equal(made[0].txt.manufacturer, "");
});
