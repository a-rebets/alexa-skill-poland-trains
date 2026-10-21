/* Station picker for the Upcoming Trains Alexa skill.
 *
 * Alexa opens this page as an OAuth 2.0 implicit-grant authorization URL.
 * There is no token endpoint. After the user taps Done, the page redirects
 * to Amazon's redirect_uri with the access token in the URL fragment.
 *
 * The token is the station list itself (see lambda/upcoming_trains/token.py).
 * It is not signed: this file is public, and the token only names public stations.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (typeof window !== "undefined") {
    window.UpcomingTrainsPicker = api;
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", function () {
        api.start(window.STATIONS || [], window.location.search);
      });
    } else {
      api.start(window.STATIONS || [], window.location.search);
    }
  }
  root.UpcomingTrainsPicker = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  var CLIENT_ID = "upcoming-trains";
  var MAX_STATIONS = 5;
  var REDIRECT_HOSTS = {
    "pitangui.amazon.com": true,
    "layla.amazon.com": true,
    "alexa.amazon.co.jp": true,
  };
  var REDIRECT_PATH = "/spa/skill/account-linking-status.html";
  var FOLDS = {
    "ą": "a", "ć": "c", "ę": "e", "ł": "l", "ń": "n",
    "ó": "o", "ś": "s", "ź": "z", "ż": "z",
  };
  var ALIASES = { warsaw: "warszawa", cracow: "krakow" };

  function normalizeName(text) {
    if (!text) return "";
    var folded = "";
    var lower = text.toLowerCase();
    for (var i = 0; i < lower.length; i++) {
      var ch = lower.charAt(i);
      folded += FOLDS[ch] || ch;
    }
    folded = folded.normalize("NFD").replace(/[\u0300-\u036f]/g, "");
    return folded.replace(/[^a-z0-9]+/g, " ").trim();
  }

  function aliasQuery(normalized) {
    if (!normalized) return "";
    return normalized.split(" ").map(function (part) {
      return ALIASES[part] || part;
    }).join(" ");
  }

  function levenshtein(left, right) {
    if (Math.abs(left.length - right.length) > 1) return 2;
    if (left === right) return 0;
    var prev = [];
    for (var j = 0; j <= right.length; j++) prev.push(j);
    for (var i = 1; i <= left.length; i++) {
      var cur = [i];
      for (var k = 1; k <= right.length; k++) {
        var cost = left.charAt(i - 1) === right.charAt(k - 1) ? 0 : 1;
        cur.push(Math.min(prev[k] + 1, cur[k - 1] + 1, prev[k - 1] + cost));
      }
      prev = cur;
    }
    return prev[prev.length - 1];
  }

  function scoreName(name, query) {
    if (!query || !name) return 0;
    if (name === query) return 10000;
    if (name.indexOf(query) === 0) return 8000 - name.length;
    var index = name.indexOf(query);
    if (index >= 0) return 6000 - index * 10 - name.length;
    var queryTokens = query.split(" ");
    var nameTokens = name.split(" ");
    var tokensMatch = queryTokens.every(function (token) {
      return nameTokens.some(function (part) { return part.indexOf(token) === 0; });
    });
    if (queryTokens.length && tokensMatch) return 5000 - name.length;
    if (queryTokens.length === 1 && query.length >= 4) {
      for (var i = 0; i < nameTokens.length; i++) {
        if (levenshtein(nameTokens[i], query) <= 1) return 3000 - name.length;
      }
    }
    return 0;
  }

  function searchStations(stations, query, limit) {
    var folded = aliasQuery(normalizeName(query));
    if (!folded) return [];
    var cap = limit || 30;
    var scored = [];
    for (var i = 0; i < stations.length; i++) {
      var station = stations[i];
      var normalized = station.norm || normalizeName(station.name);
      var score = scoreName(normalized, folded);
      if (score > 0) scored.push({ score: score, name: station.name, station: station });
    }
    scored.sort(function (a, b) {
      if (b.score !== a.score) return b.score - a.score;
      return a.name < b.name ? -1 : a.name > b.name ? 1 : 0;
    });
    return scored.slice(0, cap).map(function (item) { return item.station; });
  }

  function encodeToken(ids) {
    var json = JSON.stringify(ids);
    var bytes = new TextEncoder().encode(json);
    var binary = "";
    for (var i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
    var b64 = btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
    return "v1." + b64;
  }

  function isAllowedRedirect(uri) {
    var url;
    try {
      url = new URL(uri);
    } catch (err) {
      return false;
    }
    if (url.protocol !== "https:") return false;
    if (url.username || url.password || url.port || url.hash) return false;
    if (!REDIRECT_HOSTS[url.hostname]) return false;
    if (url.pathname !== REDIRECT_PATH) return false;
    var keys = [];
    url.searchParams.forEach(function (_value, key) { keys.push(key); });
    if (keys.length !== 1 || keys[0] !== "vendorId") return false;
    var vendor = url.searchParams.get("vendorId") || "";
    return /^[A-Za-z0-9._-]{1,80}$/.test(vendor);
  }

  function rawParam(search, name) {
    var parts = String(search || "").replace(/^\?/, "").split("&");
    for (var i = 0; i < parts.length; i++) {
      var eq = parts[i].indexOf("=");
      var key = eq < 0 ? parts[i] : parts[i].slice(0, eq);
      if (key === name) {
        var val = eq < 0 ? "" : parts[i].slice(eq + 1);
        try { return decodeURIComponent(val); } catch (err) { return val; }
      }
    }
    return "";
  }

  function validateLaunchQuery(params, search) {
    var redirect = params.get("redirect_uri") || "";
    // Amazon's state is long and may contain "+", which URLSearchParams turns into a space.
    var state = search ? rawParam(search, "state") : (params.get("state") || "");
    var client = params.get("client_id") || "";
    var responseType = params.get("response_type") || "";
    var errors = [];
    if (responseType !== "token") errors.push("response_type");
    if (client !== CLIENT_ID) errors.push("client_id");
    if (!state || state.length > 8192 || /[\s<>"]/.test(state)) errors.push("state");
    if (!isAllowedRedirect(redirect)) errors.push("redirect_uri");
    return { ok: errors.length === 0, errors: errors, redirect: redirect, state: state };
  }

  function buildRedirect(redirectUri, state, accessToken) {
    var url = new URL(redirectUri);
    var hash = new URLSearchParams();
    hash.set("state", state);
    hash.set("access_token", accessToken);
    hash.set("token_type", "Bearer");
    return url.origin + url.pathname + url.search + "#" + hash.toString();
  }

  function classify(params, search) {
    var touched = params.has("redirect_uri") || params.has("client_id") || params.has("state") || params.has("response_type");
    if (!touched) return { mode: "preview" };
    var result = validateLaunchQuery(params, search);
    if (!result.ok) return { mode: "error", errors: result.errors };
    return { mode: "link", redirect: result.redirect, state: result.state };
  }

  function start(stations, search) {
    var params = new URLSearchParams(search || "");
    var mode = classify(params, search);
    var selected = [];
    var banner = document.getElementById("banner");
    var input = document.getElementById("q");
    var results = document.getElementById("results");
    var chosen = document.getElementById("selected");
    var count = document.getElementById("count");
    var done = document.getElementById("done");
    var tokenBox = document.getElementById("token");
    if (!input || !stations) return;

    function say(text, kind) {
      banner.hidden = false;
      banner.className = "banner " + (kind || "");
      banner.textContent = text;
    }

    if (!stations.length) {
      say("The station list did not load. Rebuild it with scripts/build_data.py.", "bad");
    } else if (mode.mode === "error") {
      say(errorText(mode.errors), "bad");
    } else if (mode.mode === "preview") {
      say("Preview: Alexa did not open this page, so Done shows a token instead of redirecting. Paste it into scripts/preview.py --token.", "note");
    } else {
      say("Choose up to " + MAX_STATIONS + " stations, then tap Done to return to Alexa.", "ok");
    }

    function render() {
      count.textContent = selected.length + " of " + MAX_STATIONS;
      done.disabled = selected.length === 0 || mode.mode === "error";
      chosen.innerHTML = "";
      selected.forEach(function (station, index) {
        var item = document.createElement("li");
        var label = document.createElement("span");
        label.textContent = station.name;
        var code = document.createElement("small");
        code.textContent = station.id;
        var remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "Remove";
        remove.addEventListener("click", function () {
          selected.splice(index, 1);
          render();
          showMatches(input.value);
        });
        item.appendChild(label);
        item.appendChild(code);
        item.appendChild(remove);
        chosen.appendChild(item);
      });
      if (!selected.length) {
        var empty = document.createElement("li");
        empty.className = "empty";
        empty.textContent = "None yet.";
        chosen.appendChild(empty);
      }
    }

    function showMatches(query) {
      results.innerHTML = "";
      var matches = searchStations(stations, query, 30);
      if (!query.trim()) {
        var hint = document.createElement("li");
        hint.className = "empty";
        hint.textContent = "Type a station. Diacritics are optional, so lodz finds Łódź.";
        results.appendChild(hint);
        return;
      }
      if (!matches.length) {
        var none = document.createElement("li");
        none.className = "empty";
        none.textContent = "No station matches that.";
        results.appendChild(none);
        return;
      }
      matches.forEach(function (station) {
        var item = document.createElement("li");
        var button = document.createElement("button");
        button.type = "button";
        var already = selected.some(function (item) { return item.id === station.id; });
        button.innerHTML = "";
        var name = document.createElement("span");
        name.textContent = station.name;
        var code = document.createElement("small");
        code.textContent = station.id + (already ? " · selected" : "");
        button.appendChild(name);
        button.appendChild(code);
        button.disabled = already;
        button.addEventListener("click", function () {
          if (selected.some(function (item) { return item.id === station.id; })) return;
          if (selected.length >= MAX_STATIONS) {
            say("Five stations is the limit, so the announcement stays short.", "note");
            return;
          }
          selected.push(station);
          render();
          showMatches(input.value);
        });
        item.appendChild(button);
        results.appendChild(item);
      });
    }

    input.addEventListener("input", function () { showMatches(input.value); });
    done.addEventListener("click", function () {
      var ids = selected.map(function (station) { return station.id; });
      var token = encodeToken(ids);
      if (mode.mode !== "link") {
        tokenBox.hidden = false;
        tokenBox.textContent = token;
        return;
      }
      window.location.replace(buildRedirect(mode.redirect, mode.state, token));
    });
    render();
    showMatches("");
    input.focus();
  }

  function errorText(errors) {
    if (errors.indexOf("redirect_uri") >= 0) {
      return "Alexa sent a redirect address this page does not recognize, so nothing was saved.";
    }
    if (errors.indexOf("client_id") >= 0) {
      return "The client id must be upcoming-trains. Check the account-linking settings in the Alexa console.";
    }
    if (errors.indexOf("state") >= 0) {
      return "Alexa did not send a usable state value, so this page will not redirect.";
    }
    return "Alexa did not ask for an implicit-grant token (response_type=token).";
  }

  return {
    CLIENT_ID: CLIENT_ID,
    MAX_STATIONS: MAX_STATIONS,
    normalizeName: normalizeName,
    searchStations: searchStations,
    encodeToken: encodeToken,
    isAllowedRedirect: isAllowedRedirect,
    validateLaunchQuery: validateLaunchQuery,
    buildRedirect: buildRedirect,
    classify: classify,
    start: start,
  };
});
