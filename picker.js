/* Station picker for the My Train Schedule Alexa skill.
 * The timetable currently covers only Poland. Other countries may be added later.
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

  var TRAIN_SVG = '<svg viewBox="0 0 32 32" aria-hidden="true" focusable="false"><path fill="none" stroke="currentColor" stroke-width="1.6" d="M8 12.5Q8 8 12 8h8q4 0 4 4.5V20q0 2.2-2.2 2.2H10.2Q8 22.2 8 20v-7.5z"/><rect x="11.2" y="11.2" width="9.6" height="4.4" rx="1" fill="currentColor"/><circle cx="13" cy="24.2" r="1.5" fill="currentColor"/><circle cx="19" cy="24.2" r="1.5" fill="currentColor"/></svg>';
  var CACHE = "8";
  var KEY_RE = /^[A-Za-z0-9_-]{8,128}$/;

  function b64url(json) {
    var bytes = new TextEncoder().encode(json);
    var binary = "";
    for (var i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
  }

  function encodeToken(ids) {
    return "v1." + b64url(JSON.stringify(ids));
  }

  function choiceFor(options, pressed) {
    var ids = (options || []).map(function (option) { return option.id; });
    var on = [];
    ids.forEach(function (id) {
      if (pressed[id]) on.push(id);
    });
    if (!ids.length || on.length === ids.length) return null;
    on.sort();
    return on;
  }

  function usableKey(value) {
    var key = String(value == null ? "" : value).trim();
    return KEY_RE.test(key) ? key : "";
  }

  function encodeChoices(choices, apiKey) {
    var payload = [];
    var seen = {};
    (choices || []).forEach(function (pair) {
      var id = pair[0];
      if (seen[id]) return;
      seen[id] = true;
      var dirs = pair[1];
      if (dirs == null) {
        payload.push([id, null]);
        return;
      }
      var cleaned = [];
      var have = {};
      dirs.forEach(function (dir) {
        if (!have[dir]) {
          have[dir] = true;
          cleaned.push(dir);
        }
      });
      cleaned.sort();
      payload.push([id, cleaned.length ? cleaned : null]);
    });
    var key = usableKey(apiKey);
    var body = key ? { c: payload, k: key } : payload;
    return "v2." + b64url(JSON.stringify(body));
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
    // Alexa documents vendorId, and may add other query parameters. The host
    // and path stay pinned, so an extra parameter cannot move the token.
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

  var FEATURED = [
    "warszawa centralna",
    "krakow glowny",
    "gdansk glowny",
    "wroclaw glowny",
    "poznan glowny",
    "lodz fabryczna",
  ];
  var CHECK_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M7.5 12.2l3 3.1 6-6.4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  var LIMIT_TEXT = "Five stations is the most this skill will read. Remove one to choose another.";

  function foldChar(ch) {
    var lower = ch.toLowerCase();
    var mapped = FOLDS[lower] || lower;
    return mapped.normalize("NFD").replace(/[\u0300-\u036f]/g, "");
  }

  function namePieces(name) {
    var pieces = [];
    var buf = "";
    var start = 0;
    function flush(end) {
      if (buf) pieces.push({ start: start, end: end, text: buf });
      buf = "";
    }
    for (var i = 0; i < name.length; i++) {
      var folded = foldChar(name.charAt(i));
      if (folded.length === 1 && /[a-z0-9]/.test(folded)) {
        if (!buf) start = i;
        buf += folded;
      } else {
        flush(i);
      }
    }
    flush(name.length);
    return pieces;
  }

  function rangesFor(pieces, from, to) {
    var ranges = [];
    var offset = 0;
    for (var i = 0; i < pieces.length; i++) {
      var piece = pieces[i];
      var pieceEnd = offset + piece.text.length;
      var overlapStart = Math.max(from, offset);
      var overlapEnd = Math.min(to, pieceEnd);
      if (overlapStart < overlapEnd) {
        var local = overlapStart - offset;
        ranges.push({
          start: piece.start + local,
          end: piece.start + (overlapEnd - offset),
        });
      }
      offset = pieceEnd + 1;
    }
    return ranges;
  }

  function mergeRanges(ranges) {
    var sorted = ranges.slice().sort(function (a, b) {
      return a.start - b.start || a.end - b.end;
    });
    var out = [];
    sorted.forEach(function (range) {
      var last = out[out.length - 1];
      if (last && range.start <= last.end) last.end = Math.max(last.end, range.end);
      else out.push({ start: range.start, end: range.end });
    });
    return out;
  }

  function matchRanges(name, query) {
    var folded = aliasQuery(normalizeName(query));
    if (!folded) return [];
    var pieces = namePieces(name);
    var norm = pieces.map(function (piece) { return piece.text; }).join(" ");
    var index = norm.indexOf(folded);
    if (index >= 0) return rangesFor(pieces, index, index + folded.length);
    var tokens = folded.split(" ");
    var ranges = [];
    var used = {};
    tokens.forEach(function (token) {
      var offset = 0;
      for (var i = 0; i < pieces.length; i++) {
        var part = pieces[i].text;
        var take = 0;
        if (!used[i] && part.indexOf(token) === 0) take = token.length;
        else if (!used[i] && tokens.length === 1 && token.length >= 4 && levenshtein(part, token) <= 1) take = part.length;
        if (take) {
          ranges = ranges.concat(rangesFor(pieces, offset, offset + take));
          used[i] = true;
          break;
        }
        offset += part.length + 1;
      }
    });
    return mergeRanges(ranges);
  }

  function fillHighlighted(el, name, query) {
    var ranges = mergeRanges(matchRanges(name, query));
    if (!ranges.length) {
      el.textContent = name;
      return;
    }
    var cursor = 0;
    ranges.forEach(function (range) {
      if (range.start > cursor) el.appendChild(document.createTextNode(name.slice(cursor, range.start)));
      var mark = document.createElement("mark");
      mark.textContent = name.slice(range.start, range.end);
      el.appendChild(mark);
      cursor = range.end;
    });
    if (cursor < name.length) el.appendChild(document.createTextNode(name.slice(cursor)));
  }

  function featuredStations(stations) {
    var byNorm = {};
    for (var i = 0; i < stations.length; i++) {
      var norm = stations[i].norm || normalizeName(stations[i].name);
      if (!byNorm[norm]) byNorm[norm] = stations[i];
    }
    var found = [];
    FEATURED.forEach(function (name) {
      if (byNorm[name]) found.push(byNorm[name]);
    });
    return found;
  }

  function start(stations, search) {
    var params = new URLSearchParams(search || "");
    var mode = classify(params, search);
    var selected = [];
    var step = 1;
    var directionData = null;
    var pressed = {};
    var hiddenPreview = previewHidden(params);
    var banner = document.getElementById("banner");
    var input = document.getElementById("q");
    var results = document.getElementById("results");
    var chosen = document.getElementById("selected");
    var done = document.getElementById("done");
    var tokenBox = document.getElementById("token");
    var limit = document.getElementById("limit");
    var live = document.getElementById("live");
    var choice = document.getElementById("choice");
    var form = document.getElementById("finder");
    if (!input || !results || !chosen || !done || !stations) return;

    function say(text, kind) {
      if (!banner) return;
      banner.hidden = false;
      banner.className = "banner" + (kind ? " " + kind : "");
      banner.textContent = text;
      banner.setAttribute("role", kind === "bad" ? "alert" : "status");
    }

    if (!stations.length) {
      say("The station list didn't load. Refresh the page.", "bad");
    } else if (mode.mode === "error") {
      say(errorText(mode.errors), "bad");
    } else if (mode.mode === "preview") {
      say("Opened outside Alexa, so Done stays on this page.", "note");
    }

    function isSelected(station) {
      return selected.some(function (item) { return item.id === station.id; });
    }

    function render() {
      var n = selected.length;
      done.disabled = n === 0 || mode.mode === "error";
      if (step === 1) {
        done.textContent = n === 0 ? "Next" : "Next · " + n + " station" + (n === 1 ? "" : "s");
      }
      if (limit) {
        limit.hidden = n < MAX_STATIONS;
        limit.textContent = n >= MAX_STATIONS ? LIMIT_TEXT : "";
      }
      if (choice) {
        choice.textContent = n === 0
          ? "No stations selected."
          : n + " station" + (n === 1 ? "" : "s") + " selected." + (n >= MAX_STATIONS ? " " + LIMIT_TEXT : "");
      }
      chosen.innerHTML = "";
      selected.forEach(function (station) {
        var item = document.createElement("li");
        item.className = "chip";
        var label = document.createElement("span");
        label.textContent = station.name;
        var remove = document.createElement("button");
        remove.type = "button";
        remove.setAttribute("aria-label", "Remove " + station.name);
        remove.textContent = "×";
        remove.addEventListener("click", function () {
          selected = selected.filter(function (item) { return item.id !== station.id; });
          render();
          showMatches(input.value);
        });
        item.appendChild(label);
        item.appendChild(remove);
        chosen.appendChild(item);
      });
    }

    function showMatches(query) {
      results.innerHTML = "";
      var typed = query.trim();
      var matches = typed ? orderedMatches(query) : featuredStations(stations);
      results.setAttribute("aria-label", typed ? "Search results" : "Major stations");
      if (live) {
        live.textContent = matches.length
          ? (typed ? matches.length + (matches.length === 1 ? " station." : " stations.") : matches.length + " major stations.")
          : "No stations match.";
      }
      if (!matches.length) {
        var none = document.createElement("li");
        none.className = "empty";
        var title = document.createElement("p");
        title.textContent = typed ? "No station matches that." : "Type a station name.";
        var hint = document.createElement("p");
        hint.textContent = "Diacritics are optional, so lodz finds Łódź.";
        none.appendChild(title);
        none.appendChild(hint);
        results.appendChild(none);
        return;
      }
      matches.forEach(function (station) {
        var item = document.createElement("li");
        var button = document.createElement("button");
        button.type = "button";
        var already = isSelected(station);
        button.setAttribute("aria-pressed", already ? "true" : "false");
        var name = document.createElement("span");
        name.className = "name";
        fillHighlighted(name, station.name, typed ? query : "");
        var check = document.createElement("span");
        check.className = "check";
        check.setAttribute("aria-hidden", "true");
        check.innerHTML = CHECK_SVG;
        button.appendChild(name);
        button.appendChild(check);
        button.addEventListener("click", function () {
          if (isSelected(station)) {
            selected = selected.filter(function (item) { return item.id !== station.id; });
          } else if (selected.length >= MAX_STATIONS) {
            if (limit && !limit.hidden) limit.scrollIntoView({ block: "nearest" });
            return;
          } else {
            selected.push(station);
          }
          render();
          showMatches(input.value);
        });
        item.appendChild(button);
        results.appendChild(item);
      });
    }

    function orderedMatches(query) {
      var pinned = searchStations(featuredStations(stations), query, FEATURED.length);
      var rest = searchStations(stations, query, 30);
      var seen = {};
      var out = [];
      pinned.forEach(function (station) {
        seen[station.id] = true;
        out.push(station);
      });
      rest.forEach(function (station) {
        if (!seen[station.id] && out.length < 30) out.push(station);
      });
      return out;
    }

    function onType() { showMatches(input.value); }
    input.addEventListener("input", onType);
    input.addEventListener("search", onType);
    if (form) form.addEventListener("submit", function (event) { event.preventDefault(); });
    var stepStations = document.getElementById("step-stations");
    var stepDirections = document.getElementById("step-directions");
    var stepVoice = document.getElementById("step-voice");
    var sections = document.getElementById("direction-sections");
    var hint = document.getElementById("direction-hint");
    var directionLive = document.getElementById("direction-live");
    var title = document.getElementById("directions-title");
    var voiceTitle = document.getElementById("voice-title");
    var keyInput = document.getElementById("eleven-key");
    var voiceHint = document.getElementById("voice-hint");
    var voiceLive = document.getElementById("voice-live");

    function showStations() {
      step = 1;
      if (stepStations) stepStations.hidden = false;
      if (stepDirections) stepDirections.hidden = true;
      if (stepVoice) stepVoice.hidden = true;
      if (back) back.hidden = true;
      if (hint) hint.hidden = true;
      render();
      input.focus();
    }

    function setHint(text) {
      if (hint) {
        hint.hidden = !text;
        hint.textContent = text || "";
      }
      if (directionLive) directionLive.textContent = text || "";
    }

    function paintDirections() {
      if (!sections) return;
      sections.innerHTML = "";
      selected.forEach(function (station) {
        var options = (directionData && directionData[station.id]) || [];
        var block = document.createElement("section");
        block.className = "station-block";
        var heading = document.createElement("h2");
        heading.id = "station-" + station.id;
        heading.textContent = station.name;
        var list = document.createElement("ul");
        list.className = "directions";
        list.setAttribute("role", "group");
        list.setAttribute("aria-labelledby", heading.id);
        block.appendChild(heading);
        if (!options.length) {
          var empty = document.createElement("p");
          empty.className = "loading";
          empty.textContent = "Every train from this station.";
          block.appendChild(empty);
        }
        options.forEach(function (option) {
          var item = document.createElement("li");
          var button = document.createElement("button");
          button.type = "button";
          button.className = "direction";
          var on = !!(pressed[station.id] && pressed[station.id][option.id]);
          button.setAttribute("aria-pressed", on ? "true" : "false");
          var icon = document.createElement("span");
          icon.className = "train";
          icon.innerHTML = TRAIN_SVG;
          var copy = document.createElement("span");
          copy.className = "direction-copy";
          var name = document.createElement("span");
          name.className = "direction-name";
          name.textContent = option.name;
          copy.appendChild(name);
          if (option.lines && option.lines.length) {
            var lines = document.createElement("span");
            lines.className = "line-list";
            lines.textContent = option.lines.join(" · ");
            copy.appendChild(lines);
          }
          var check = document.createElement("span");
          check.className = "check";
          check.setAttribute("aria-hidden", "true");
          check.innerHTML = CHECK_SVG;
          button.appendChild(icon);
          button.appendChild(copy);
          button.appendChild(check);
          button.addEventListener("click", function () {
            toggleDirection(station, option, options);
          });
          item.appendChild(button);
          list.appendChild(item);
        });
        block.appendChild(list);
        sections.appendChild(block);
      });
    }

    function toggleDirection(station, option, options) {
      var state = pressed[station.id];
      if (!state) return;
      var turningOff = !!state[option.id];
      if (turningOff) {
        var still = options.some(function (item) {
          return item.id !== option.id && state[item.id];
        });
        if (!still) {
          setHint("Keep at least one direction for " + station.name + ".");
          return;
        }
      }
      state[option.id] = !turningOff;
      setHint("");
      if (directionLive) {
        directionLive.textContent = option.name + (state[option.id] ? " on." : " off.");
      }
      paintDirections();
    }

    function armPressed() {
      selected.forEach(function (station) {
        var options = (directionData && directionData[station.id]) || [];
        var state = {};
        var hide = hiddenPreview[station.id] || {};
        options.forEach(function (option) {
          state[option.id] = !hide[option.id];
        });
        var any = options.some(function (option) { return state[option.id]; });
        if (options.length && !any) {
          state[options[0].id] = true;
        }
        pressed[station.id] = state;
      });
    }

    function showDirectionStep() {
      step = 2;
      if (stepStations) stepStations.hidden = true;
      if (stepDirections) stepDirections.hidden = false;
      if (stepVoice) stepVoice.hidden = true;
      if (back) back.hidden = false;
      done.disabled = false;
      done.textContent = "Next";
      paintDirections();
      if (title && title.focus) title.focus();
    }

    function refreshKeyHint() {
      var raw = keyInput ? keyInput.value.trim() : "";
      var text = raw && !KEY_RE.test(raw)
        ? "That isn't a usable key. Clear it, or paste the key from ElevenLabs. Done will use the Alexa voice."
        : "";
      if (voiceHint) {
        voiceHint.hidden = !text;
        voiceHint.textContent = text;
      }
      if (voiceLive) voiceLive.textContent = text;
    }

    function showVoice() {
      step = 3;
      if (stepStations) stepStations.hidden = true;
      if (stepDirections) stepDirections.hidden = true;
      if (stepVoice) stepVoice.hidden = false;
      if (back) back.hidden = false;
      done.disabled = false;
      done.textContent = "Done";
      refreshKeyHint();
      if (voiceTitle && voiceTitle.focus) voiceTitle.focus();
    }

    function openDirections() {
      if (!selected.length || !stepDirections) {
        finish();
        return;
      }
      step = 2;
      if (stepStations) stepStations.hidden = true;
      if (stepDirections) stepDirections.hidden = false;
      if (stepVoice) stepVoice.hidden = true;
      if (back) back.hidden = false;
      done.disabled = true;
      done.textContent = "Next";
      if (sections) {
        sections.innerHTML = "";
        var waiting = document.createElement("p");
        waiting.className = "loading";
        waiting.textContent = "Loading directions.";
        sections.appendChild(waiting);
      }
      loadDirections().then(function (data) {
        directionData = data || {};
        armPressed();
        showDirectionStep();
      }, function () {
        directionData = {};
        armPressed();
        setHint("Directions didn't load. Next includes every train.");
        showDirectionStep();
      });
    }

    function finish() {
      var choices = selected.map(function (station) {
        var options = (directionData && directionData[station.id]) || [];
        var state = pressed[station.id] || {};
        return [station.id, choiceFor(options, state)];
      });
      var voiceKey = mode.mode === "link" && keyInput ? keyInput.value : "";
      var token = choices.length ? encodeChoices(choices, voiceKey) : encodeToken(selected.map(function (station) { return station.id; }));
      if (mode.mode !== "link") {
        if (tokenBox) {
          tokenBox.hidden = false;
          tokenBox.textContent = token;
        }
        return;
      }
      window.location.replace(buildRedirect(mode.redirect, mode.state, token));
    }

    var back = document.getElementById("back");
    done.addEventListener("click", function () {
      if (done.disabled) return;
      if (step === 1) openDirections();
      else if (step === 2) showVoice();
      else finish();
    });
    if (keyInput) keyInput.addEventListener("input", refreshKeyHint);
    if (back) {
      back.addEventListener("click", function () {
        if (step === 3) showDirectionStep();
        else showStations();
      });
    }
    render();
    showMatches("");
    var preset = previewSelection(stations, params, mode);
    if (preset) {
      selected = preset;
      render();
      showMatches(input.value);
      openDirections();
    }
  }

  function previewHidden(params) {
    var map = {};
    String(params.get("hide") || "").split(",").forEach(function (part) {
      var bits = part.split(":");
      if (bits.length !== 2 || !bits[0] || !bits[1]) return;
      if (!map[bits[0]]) map[bits[0]] = {};
      map[bits[0]][bits[1]] = true;
    });
    return map;
  }

  function previewSelection(stations, params, mode) {
    if (!mode || mode.mode !== "preview") return null;
    var raw = params.get("stations") || "";
    if (!raw) return null;
    var byId = {};
    stations.forEach(function (station) { byId[station.id] = station; });
    var picked = [];
    var seen = {};
    raw.split(",").forEach(function (id) {
      id = id.trim();
      if (!id || seen[id] || !byId[id] || picked.length >= MAX_STATIONS) return;
      seen[id] = true;
      picked.push(byId[id]);
    });
    return picked.length ? picked : null;
  }

  var directionsPromise = null;

  function loadDirections() {
    if (!directionsPromise) {
      var pending = fetch("directions.json?v=" + CACHE).then(function (response) {
        if (!response.ok) throw new Error("directions");
        return response.json();
      });
      directionsPromise = pending.then(function (data) {
        return data;
      }, function (err) {
        directionsPromise = null;
        throw err;
      });
    }
    return directionsPromise;
  }

  function errorText(errors) {
    if (errors.indexOf("redirect_uri") >= 0) {
      return "This link from Alexa isn't one this page can use. Open the skill in the Alexa app and try again.";
    }
    if (errors.indexOf("client_id") >= 0) {
      return "Alexa opened this page for a different skill, so it can't continue.";
    }
    if (errors.indexOf("state") >= 0) {
      return "Alexa didn't finish opening this page, so your choice can't be sent back. Close it and link the account again.";
    }
    return "Alexa asked for a different kind of sign-in. This page only works when account linking is set to Implicit Grant.";
  }

  return {
    CLIENT_ID: CLIENT_ID,
    MAX_STATIONS: MAX_STATIONS,
    normalizeName: normalizeName,
    searchStations: searchStations,
    encodeToken: encodeToken,
    encodeChoices: encodeChoices,
    choiceFor: choiceFor,
    isAllowedRedirect: isAllowedRedirect,
    validateLaunchQuery: validateLaunchQuery,
    buildRedirect: buildRedirect,
    classify: classify,
    start: start,
  };
});
