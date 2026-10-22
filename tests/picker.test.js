const test = require("node:test");
const assert = require("node:assert/strict");
const picker = require("../picker/picker.js");

const STATIONS = [
  { id: "1", name: "Łódź Fabryczna" },
  { id: "2", name: "Łódź Widzew" },
  { id: "3", name: "Warszawa Centralna" },
  { id: "4", name: "Warszawa Zachodnia" },
  { id: "5", name: "Kraków Główny" },
];

test("lodz and warsaw central resolve", () => {
  assert.equal(picker.normalizeName("Łódź Fabryczna"), "lodz fabryczna");
  const lodz = picker.searchStations(STATIONS, "lodz").map((s) => s.name);
  assert.ok(lodz[0].startsWith("Łódź"));
  assert.ok(lodz.includes("Łódź Fabryczna"));
  const warsaw = picker.searchStations(STATIONS, "warsaw central");
  assert.equal(warsaw[0].id, "3");
  assert.deepEqual(picker.searchStations(STATIONS, ""), []);
});

test("implicit grant redirect is only the three Alexa hosts", () => {
  const good = "https://layla.amazon.com/spa/skill/account-linking-status.html?vendorId=M2ABCDEF";
  assert.equal(picker.isAllowedRedirect(good), true);
  assert.equal(
    picker.isAllowedRedirect("https://pitangui.amazon.com/api/skill/link/M2ABCDEF"),
    false,
  );
  assert.equal(picker.isAllowedRedirect("http://layla.amazon.com/spa/skill/account-linking-status.html?vendorId=M2ABCDEF"), false);
  assert.equal(picker.isAllowedRedirect("https://evil.com/spa/skill/account-linking-status.html?vendorId=M2ABCDEF"), false);
  assert.equal(picker.isAllowedRedirect(good + "&next=https://evil.com"), true);
  assert.equal(
    picker.isAllowedRedirect("https://alexa.amazon.co.jp/spa/skill/account-linking-status.html?vendorId=M2ABCDEF"),
    true,
  );
  assert.equal(
    picker.isAllowedRedirect("https://layla.amazon.com/spa/skill/account-linking-status.html"),
    false,
  );
  assert.equal(picker.isAllowedRedirect("https://user:pass@layla.amazon.com/spa/skill/account-linking-status.html?vendorId=M2ABCDEF"), false);
});

test("done builds a fragment, not a query token", () => {
  const params = new URLSearchParams({
    state: "abcXYZ",
    client_id: "upcoming-trains",
    response_type: "token",
    redirect_uri: "https://pitangui.amazon.com/spa/skill/account-linking-status.html?vendorId=vendor.id.1",
    scope: "stations",
  });
  const checked = picker.validateLaunchQuery(params);
  assert.equal(checked.ok, true);
  const token = picker.encodeToken(["33605", "33506"]);
  const dest = picker.buildRedirect(checked.redirect, checked.state, token);
  assert.ok(dest.startsWith("https://pitangui.amazon.com/spa/skill/account-linking-status.html?vendorId=vendor.id.1#"));
  assert.ok(dest.includes("state=abcXYZ"));
  assert.ok(dest.includes("access_token=" + token));
  assert.ok(dest.includes("token_type=Bearer"));
  assert.equal(dest.indexOf("#") < dest.indexOf("access_token="), true);
  const bad = picker.validateLaunchQuery(new URLSearchParams({
    state: "abc",
    client_id: "wrong",
    response_type: "code",
    redirect_uri: "https://layla.amazon.com/api/skill/link/M2ABCDEF",
  }));
  assert.equal(bad.ok, false);
  assert.ok(bad.errors.includes("redirect_uri"));
  assert.ok(bad.errors.includes("client_id"));
  assert.ok(bad.errors.includes("response_type"));
});

test("v2 keeps every direction as null and sorts a partial list", () => {
  const options = [{ id: "33530" }, { id: "LD" }, { id: "33605" }];
  const allOn = { "33530": true, LD: true, "33605": true };
  assert.equal(picker.choiceFor(options, allOn), null);
  assert.equal(picker.choiceFor([], {}), null);
  const partial = picker.choiceFor(options, { "33530": true, LD: false, "33605": true });
  assert.deepEqual(partial, ["33530", "33605"]);
  const token = picker.encodeChoices([
    ["33506", ["LD", "33530"]],
    ["33548", null],
    ["33506", ["999"]],
  ]);
  assert.ok(token.startsWith("v2."));
  assert.ok(token.length < 2048);
  const payload = JSON.parse(Buffer.from(token.slice(3).replace(/-/g, "+").replace(/_/g, "/"), "base64").toString());
  assert.deepEqual(payload, [
    ["33506", ["33530", "LD"]],
    ["33548", null],
  ]);
});

test("a voice key is a v2 field and a blank key stays the old array", () => {
  const choices = [["33506", null]];
  const plain = picker.encodeChoices(choices);
  assert.equal(picker.encodeChoices(choices, "   "), plain);
  assert.equal(picker.encodeChoices(choices, "no"), plain);
  const open = JSON.parse(Buffer.from(plain.slice(3).replace(/-/g, "+").replace(/_/g, "/"), "base64").toString());
  assert.ok(Array.isArray(open));
  const keyed = picker.encodeChoices(choices, "sk_test_key_123456");
  const body = JSON.parse(Buffer.from(keyed.slice(3).replace(/-/g, "+").replace(/_/g, "/"), "base64").toString());
  assert.deepEqual(body, { c: [["33506", null]], k: "sk_test_key_123456" });
  assert.ok(!plain.includes("sk_test_key_123456"));
});

test("long amazon state with plus round-trips into the fragment", () => {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=";
  let state = "";
  while (state.length < 1000) state += alphabet;
  state = state.slice(0, 1000);
  assert.ok(state.includes("+"));
  assert.ok(state.includes("/"));
  assert.ok(state.includes("="));

  const redirect = "https://layla.amazon.com/spa/skill/account-linking-status.html?vendorId=M2ABCDEF&stage=development";
  const search = [
    "state=" + state,
    "client_id=upcoming-trains",
    "response_type=token",
    "scope=stations",
    "redirect_uri=" + encodeURIComponent(redirect),
  ].join("&");

  const params = new URLSearchParams(search);
  assert.notEqual(params.get("state"), state);

  const checked = picker.validateLaunchQuery(params, search);
  assert.equal(checked.ok, true);
  assert.equal(checked.state, state);
  assert.equal(picker.classify(params, search).state, state);

  const token = picker.encodeToken(["33605"]);
  const dest = picker.buildRedirect(checked.redirect, checked.state, token);
  const hash = dest.slice(dest.indexOf("#") + 1);
  const fragment = new URLSearchParams(hash);
  assert.equal(fragment.get("state"), state);
  assert.equal(fragment.get("access_token"), token);
  assert.equal(fragment.get("token_type"), "Bearer");
  assert.ok(dest.startsWith("https://layla.amazon.com/spa/skill/account-linking-status.html?"));
  assert.ok(dest.includes("vendorId=M2ABCDEF"));
  assert.ok(dest.includes("stage=development"));

  const tooLong = "a".repeat(8193);
  const rejected = picker.validateLaunchQuery(
    new URLSearchParams({
      state: tooLong,
      client_id: "upcoming-trains",
      response_type: "token",
      redirect_uri: "https://pitangui.amazon.com/spa/skill/account-linking-status.html?vendorId=M2ABCDEF",
    }),
    "state=" + tooLong,
  );
  assert.equal(rejected.ok, false);
  assert.ok(rejected.errors.includes("state"));
});
