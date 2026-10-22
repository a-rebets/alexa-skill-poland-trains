const test = require("node:test");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");
const PORT = 43127;
const DEBUG = 9333;
const ARTIFACTS = "/opt/cursor/artifacts";

test("direction step is a column of pressable cards", async () => {
  const server = spawn(
    "python3",
    ["-m", "http.server", String(PORT), "--bind", "127.0.0.1"],
    { cwd: path.join(ROOT, "picker"), stdio: "ignore" },
  );
  const chrome = spawn(
    "google-chrome",
    [
      "--headless=new",
      "--disable-gpu",
      "--no-sandbox",
      "--disable-dev-shm-usage",
      "--remote-debugging-port=" + DEBUG,
      "--user-data-dir=/tmp/picker-chrome-" + Date.now(),
      "about:blank",
    ],
    { stdio: "ignore" },
  );
  try {
    await waitFor("http://127.0.0.1:" + PORT + "/index.html");
    await waitFor("http://127.0.0.1:" + DEBUG + "/json/version");
    const targets = await (await fetch("http://127.0.0.1:" + DEBUG + "/json/list")).json();
    const page = targets.find((target) => target.type === "page");
    if (!page) throw new Error("chrome opened no page");
    const ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      ws.addEventListener("open", resolve);
      ws.addEventListener("error", reject);
    });
    const cdp = new Cdp(ws);
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: 390,
      height: 844,
      deviceScaleFactor: 2,
      mobile: true,
    });
    const url =
      "http://127.0.0.1:" + PORT + "/index.html?stations=33506,33548";
    await cdp.send("Page.navigate", { url });
    const ready = await poll(cdp, () => document.querySelectorAll(".direction").length > 3);
    assert.equal(ready.title, "Choose directions");
    assert.deepEqual(ready.stations, ["Warszawa Zachodnia", "Warszawa Ursus Północny"]);
    assert.equal(ready.backVisible, true);
    assert.equal(ready.done, "Next");
    assert.ok(ready.total >= 2);
    assert.equal(ready.pressed, ready.total);
    assert.ok(ready.sample.some((card) => card.lines.includes("S2") || card.name === "Long-distance (Intercity)"));
    assert.ok(ready.sample.every((card) => card.pressed === "true"));
    await shot(cdp, path.join(ARTIFACTS, "directions-two-stations.png"));

    await cdp.send("Runtime.evaluate", {
      expression: "document.querySelector('#done').click()",
    });
    const voice = await poll(cdp, () => !document.getElementById("step-voice").hidden);
    assert.equal(voice.voiceTitle, "Natural voice (optional)");
    assert.equal(voice.done, "Done");
    assert.equal(voice.keyType, "password");
    assert.equal(voice.keyValue, "");
    assert.match(voice.voiceCopy, /billed to your key/);
    assert.match(voice.voiceCopy, /standard Alexa voice/);
    assert.match(voice.voiceLink, /^https:\/\/elevenlabs\.io\/?$/);
    await shot(cdp, path.join(ARTIFACTS, "natural-voice.png"));
    await cdp.send("Runtime.evaluate", {
      expression: "document.querySelector('#back').click()",
    });
    await poll(cdp, () => document.getElementById("step-directions").hidden === false);

    await cdp.send("Runtime.evaluate", {
      expression: "document.querySelector('#back').click()",
    });
    const back = await poll(cdp, () => !document.getElementById("step-stations").hidden);
    assert.equal(back.step1, true);
    assert.equal(back.done.startsWith("Next"), true);

    await cdp.send("Runtime.evaluate", {
      expression: "document.querySelector('#done').click()",
    });
    await poll(cdp, () => document.querySelectorAll(".direction").length > 3);

    for (let i = 0; i < 2; i++) {
      await cdp.send("Runtime.evaluate", {
        expression:
          "document.querySelector('#step-directions .station-block:first-child .direction[aria-pressed=\"true\"]').click()",
      });
    }
    const mixed = await poll(
      cdp,
      () => document.querySelectorAll('#step-directions .direction[aria-pressed="false"]').length >= 2,
    );
    assert.ok(mixed.off >= 2);
    assert.ok(mixed.on >= 1);
    assert.equal(mixed.live.length > 0, true);
    await shot(cdp, path.join(ARTIFACTS, "directions-some-off.png"));

    const guarded = await cdp.send("Runtime.evaluate", {
      expression: `
        (function () {
          var guard = 0;
          while (guard < 40) {
            var block = document.querySelector("#step-directions .station-block");
            var ons = [];
            block.querySelectorAll(".direction").forEach(function (button) {
              if (button.getAttribute("aria-pressed") === "true") ons.push(button);
            });
            if (ons.length <= 1) {
              ons[0].click();
              block = document.querySelector("#step-directions .station-block");
              return {
                hint: document.getElementById("direction-hint").textContent,
                stillOn: block.querySelectorAll('[aria-pressed="true"]').length,
              };
            }
            ons[0].click();
            guard += 1;
          }
          return { hint: "", stillOn: 0 };
        })()
      `,
      returnByValue: true,
    });
    assert.match(guarded.result.value.hint, /Keep at least one direction/);
    assert.equal(guarded.result.value.stillOn, 1);
    ws.close();
  } finally {
    server.kill("SIGKILL");
    chrome.kill("SIGKILL");
  }
});

async function shot(cdp, file) {
  const png = await cdp.send("Page.captureScreenshot", {
    format: "png",
    captureBeyondViewport: true,
  });
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, Buffer.from(png.data, "base64"));
}

async function poll(cdp, fn) {
  const source = "(" + fn.toString() + ")()";
  for (let i = 0; i < 50; i++) {
    const result = await cdp.send("Runtime.evaluate", {
      expression: `
        (function () {
          var value = ${source};
          if (!value) return null;
          var cards = Array.prototype.slice.call(document.querySelectorAll(".direction"));
          return {
            title: (document.getElementById("directions-title") || {}).textContent || "",
            stations: Array.prototype.map.call(document.querySelectorAll(".station-block h2"), function (node) { return node.textContent; }),
            pressed: cards.filter(function (card) { return card.getAttribute("aria-pressed") === "true"; }).length,
            off: cards.filter(function (card) { return card.getAttribute("aria-pressed") === "false"; }).length,
            on: cards.filter(function (card) { return card.getAttribute("aria-pressed") === "true"; }).length,
            total: cards.length,
            backVisible: document.getElementById("back") ? document.getElementById("back").hidden === false : false,
            done: (document.getElementById("done") || {}).textContent || "",
            step1: document.getElementById("step-stations") ? document.getElementById("step-stations").hidden === false : false,
            live: (document.getElementById("direction-live") || {}).textContent || "",
            voiceTitle: (document.getElementById("voice-title") || {}).textContent || "",
            keyType: (document.getElementById("eleven-key") || {}).type || "",
            keyValue: (document.getElementById("eleven-key") || {}).value || "",
            voiceLink: (document.querySelector("#step-voice a") || {}).href || "",
            voiceCopy: (document.querySelector("#step-voice .lede") || {}).textContent || "",
            sample: cards.slice(0, 6).map(function (card) {
              return {
                name: card.querySelector(".direction-name").textContent,
                lines: (card.querySelector(".line-list") || {}).textContent || "",
                pressed: card.getAttribute("aria-pressed"),
              };
            }),
          };
        })()
      `,
      returnByValue: true,
    });
    if (result.result && result.result.value) return result.result.value;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("direction step did not render");
}

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.next = 1;
    this.pending = new Map();
    ws.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (!message.id || !this.pending.has(message.id)) return;
      const waiting = this.pending.get(message.id);
      this.pending.delete(message.id);
      if (message.error) waiting.reject(new Error(JSON.stringify(message.error)));
      else waiting.resolve(message.result);
    });
  }

  send(method, params) {
    const id = this.next++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params: params || {} }));
    });
  }
}

async function waitFor(url) {
  for (let i = 0; i < 50; i++) {
    try {
      const response = await fetch(url);
      if (response.status < 500) return;
    } catch (err) {
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }
  throw new Error("timed out waiting for " + url);
}
