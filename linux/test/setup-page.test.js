import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

// The phone setup page, shared with the Android app; its translations live in the page itself.
const page = readFileSync(new URL("../../android/app/src/main/assets/setup.html", import.meta.url), "utf8");
const translations = vm.runInNewContext(`(${page.match(/const TRANSLATIONS = (\{[\s\S]*?\n\});\n<\/script>/)[1]})`);

// English for every key: the contents of data-t elements, data-t-placeholder attributes and t() calls.
const english = new Map();
for (const match of page.matchAll(/<(\w+)[^>]*\sdata-t="(\w+)"[^>]*>/g)) {
  const from = match.index + match[0].length;
  english.set(match[2], page.slice(from, page.indexOf(`</${match[1]}>`, from)));
}
for (const match of page.matchAll(/placeholder="([^"]*)" data-t-placeholder="(\w+)"/g)) english.set(match[2], match[1]);
for (const match of page.matchAll(/\bt\("(\w+)", (?:"((?:[^"\\]|\\.)*)"|document\.title)\)/g)) {
  english.set(match[1], match[2]);
}
english.set("page_title", page.match(/<title>(.*)<\/title>/)[1]);
const tags = (html) => [...html.matchAll(/<[^>]+>/g)].map((m) => m[0]).sort();

test("the page has English for the keys it translates", () => {
  assert.ok(english.size > 50, `only ${english.size} keys found`);
  assert.equal(english.get("start_add"), "Under <em>Discovered</em>, choose <strong>Add</strong> next to this TV.");
  assert.equal(english.get("cant_reach"), "Can't reach the TV.");
});

test("every language translates every string and nothing else", () => {
  assert.ok(Object.keys(translations).length >= 10);
  for (const [language, strings] of Object.entries(translations)) {
    assert.deepEqual(Object.keys(strings).sort(), [...english.keys()].sort(), language);
  }
});

test("translations keep the markup the page's script looks for", () => {
  for (const [language, strings] of Object.entries(translations)) {
    for (const [key, text] of Object.entries(strings)) {
      assert.deepEqual(tags(text), tags(english.get(key)), `${language}.${key}`);
      assert.ok(text.trim() && !/<script|\son\w+=/i.test(text), `${language}.${key}`);
    }
  }
});
