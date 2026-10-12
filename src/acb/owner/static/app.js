/* ACB owner console (process P1, spec 11.1-11.2). Vanilla JS, no build step, no CDN.
 *
 * Output encoding (mandatory): every string that did not originate in this file (agent names, memos,
 * descriptions, messages, LLM replies, reason details, event data) is inserted with textContent or
 * with setAttribute on a small allow-list of safe attributes. This file never uses the HTML-parsing
 * DOM APIs and never registers handlers through attributes; the CSP forbids inline scripts anyway.
 *
 * Confirm cards come from the console server, which rebuilt them from the chain and from invoices it
 * verified itself; this page only displays them and sends back the review id (prepareId).
 */
(function () {
  "use strict";

  var root = typeof window !== "undefined" ? window : globalThis;
  var doc = root.document;

  // ------------------------------------------------------------------ DOM helpers (text only)

  var SAFE_ATTRS = {
    "class": true, "id": true, "href": true, "title": true, "type": true, "name": true, "for": true,
    "placeholder": true, "autocomplete": true, "role": true, "rel": true, "target": true, "value": true,
    "max": true, "min": true, "maxlength": true, "colspan": true, "aria-label": true, "aria-live": true,
    "data-order": true, "data-draft": true, "data-kind": true
  };

  function byId(id) { return doc.getElementById(id); }

  function txt(value) { return doc.createTextNode(value === null || value === undefined ? "" : String(value)); }

  function setAttr(node, name, value) {
    if (!SAFE_ATTRS[name]) { throw new Error("attribute not allowed: " + name); }
    if (name === "href" && safeUrl(value) === null) { return; }
    node.setAttribute(name, String(value));
  }

  /* el("div", {"class": "x"}, ["text", child]) - children are strings (as text) or nodes */
  function el(tag, attrs, children) {
    var node = doc.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        if (attrs[k] !== null && attrs[k] !== undefined && attrs[k] !== false) { setAttr(node, k, attrs[k]); }
      });
    }
    append(node, children);
    return node;
  }

  function append(node, children) {
    if (children === null || children === undefined) { return node; }
    if (!Array.isArray(children)) { children = [children]; }
    children.forEach(function (c) {
      if (c === null || c === undefined || c === false) { return; }
      node.appendChild(typeof c === "object" ? c : txt(c));
    });
    return node;
  }

  function clear(node) { while (node && node.firstChild) { node.removeChild(node.firstChild); } }

  function safeUrl(url) {
    if (typeof url !== "string" || url.length > 500) { return null; }
    if (/^https:\/\/[A-Za-z0-9.-]+(:[0-9]+)?\/[A-Za-z0-9._~:\/?#@!$&'()*+,;=%-]*$/.test(url)) { return url; }
    return null;
  }

  function isHash(value) { return typeof value === "string" && /^0x[0-9a-fA-F]{64}$/.test(value); }

  function isAddress(value) { return typeof value === "string" && /^0x[0-9a-fA-F]{40}$/.test(value); }

  function short(value) {
    var s = String(value === null || value === undefined ? "" : value);
    return s.length > 14 ? s.slice(0, 8) + "..." + s.slice(-4) : s;
  }

  // ------------------------------------------------------------------ state

  var S = {
    csrf: "",
    snapshot: null,
    events: [],
    seen: {},
    reasonTexts: {},
    explorerTx: null,
    fallback: false,
    llm: null,
    sentMessages: [],
    shownApprovals: {},
    shownDrafts: {},
    snapshotTimer: null
  };

  // ------------------------------------------------------------------ formatting

  /* base units (decimal string) -> "0.40"; exact string arithmetic, never a float */
  function usdc(value) {
    var s = String(value === null || value === undefined ? "" : value);
    if (!/^[0-9]+$/.test(s)) { return "?"; }
    while (s.length < 7) { s = "0" + s; }
    var whole = s.slice(0, -6).replace(/^0+(?=[0-9])/, "");
    var frac = s.slice(-6);
    return frac.slice(2) === "0000" ? whole + "." + frac.slice(0, 2) : whole + "." + frac;
  }

  function chainTime(ts) {
    if (typeof ts !== "number" || !isFinite(ts) || ts <= 0) { return "unknown"; }
    return new Date(ts * 1000).toISOString().replace("T", " ").replace(/\.000Z$/, "Z");
  }

  function age(seconds) {
    if (typeof seconds !== "number" || !isFinite(seconds)) { return "unknown"; }
    if (seconds < 120) { return Math.max(0, Math.floor(seconds)) + " s"; }
    if (seconds < 7200) { return Math.floor(seconds / 60) + " min"; }
    if (seconds < 172800) { return Math.floor(seconds / 3600) + " h"; }
    return Math.floor(seconds / 86400) + " days";
  }

  function eventTime(ts) {
    var s = String(ts || "");
    var m = /T([0-9:]{8})/.exec(s);
    return m ? m[1] : s.slice(0, 19);
  }

  function gradeBadge(grade) {
    var g = String(grade || "NONE");
    if (!/^(A|B|C|D|NEW|NONE)$/.test(g)) { g = "NONE"; }
    return el("span", {"class": "grade grade-" + g, "title": "grade " + g}, g);
  }

  function tierBadge(tier, frozen) {
    if (frozen || tier === 255) { return el("span", {"class": "tier tier-frozen"}, "FROZEN"); }
    return el("span", {"class": "tier"}, "Tier " + String(tier));
  }

  function reasonText(code) {
    var t = S.reasonTexts[String(code)];
    return t ? String(code) + ": " + t : String(code);
  }

  function reasonList(codes) {
    var list = el("div", {"class": "reasons"});
    (codes || []).forEach(function (c) {
      var code = (c && typeof c === "object") ? c.code : c;
      var text = (c && typeof c === "object" && c.text) ? String(code) + ": " + c.text : reasonText(code);
      if (code !== null && code !== undefined && code !== 0) { append(list, el("div", null, text)); }
    });
    return list;
  }

  function txLink(hash, explorer) {
    if (!isHash(hash)) { return el("span", {"class": "muted"}, "off-chain"); }
    var url = safeUrl(explorer) || (S.explorerTx ? safeUrl(S.explorerTx.replace("{hash}", hash)) : null);
    if (url) { return el("a", {"href": url, "target": "_blank", "rel": "noopener noreferrer", "class": "hash"}, short(hash)); }
    return el("span", {"class": "hash", "title": hash}, short(hash));
  }

  function kv(pairs) {
    var dl = el("dl", {"class": "kv"});
    pairs.forEach(function (p) {
      if (!p) { return; }
      append(dl, [el("dt", null, p[0]), el("dd", null, p[1])]);
    });
    return dl;
  }

  // ------------------------------------------------------------------ HTTP

  function api(path, body) {
    var headers = {"Content-Type": "application/json", "X-ACB-CSRF": S.csrf};
    return root.fetch(path, {
      method: "POST", headers: headers, credentials: "same-origin", body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) {
          var err = new Error((data && typeof data.detail === "string") ? data.detail : "HTTP " + r.status);
          err.data = data;
          throw err;
        }
        return data;
      });
    });
  }

  function getJson(path) {
    return root.fetch(path, {credentials: "same-origin"}).then(function (r) {
      if (!r.ok) { throw new Error("HTTP " + r.status); }
      return r.json();
    });
  }

  // ------------------------------------------------------------------ banner

  function renderBanner(snap) {
    snap = snap || {};
    var net = snap.network || {};
    var nb = byId("banner-network");
    if (nb) { nb.textContent = net.name ? "network: " + net.name + " (" + net.chainId + ")" : ""; }
    var agent = snap.agent || {};
    var llm = S.llm || agent.llm || null;
    var lb = byId("banner-llm");
    if (lb) {
      if (!agent.online && !S.llm) { lb.textContent = "agent offline"; }
      else if (llm) {
        var mode = String(llm.mode || "");
        var label = mode === "mock" ? "LLM: mock " + String(llm.persona || "careful")
          : "LLM: " + mode + " " + String(llm.model || "");
        if (llm.maskFlaggedSpans === false) { label += " (masking off)"; }
        lb.textContent = label;
      } else { lb.textContent = ""; }
    }
    var ob = byId("banner-owner");
    if (ob) {
      var owner = snap.owner || {};
      ob.textContent = owner.address ? ("owner " + short(owner.address) + (owner.isVaultOwner ? "" : " (NOT the vault owner)") +
        (snap.signing === "browser" ? ", signs in browser" : "")) : "";
      ob.className = owner.address && !owner.isVaultOwner ? "pill pill-bad" : "pill";
    }
    var fb = byId("banner-fallback");
    if (fb) { fb.hidden = !S.fallback; }
    var feeds = byId("banner-feeds");
    if (feeds) {
      clear(feeds);
      (snap.feeds || []).forEach(function (f) {
        append(feeds, el("span", {"class": f.online ? "feed-on" : "feed-off", "title": f.error || "online"},
          String(f.name) + (f.online ? " " : " (offline) ")));
      });
    }
  }

  // ------------------------------------------------------------------ panel 1: wallet

  function latestOf(events, type) {
    for (var i = events.length - 1; i >= 0; i--) {
      if (events[i].event && events[i].event.type === type) { return events[i].event; }
    }
    return null;
  }

  function renderWallet(snap, events) {
    var body = byId("wallet-body");
    var actions = byId("wallet-actions");
    if (!body) { return; }
    clear(body);
    if (actions) { clear(actions); }
    var w = snap && snap.wallet;
    if (!w) {
      var tierEv = latestOf(events || [], "tier");
      var decEv = latestOf(events || [], "decision");
      var tier = tierEv && typeof tierEv.data.toTier === "number" ? tierEv.data.toTier
        : (decEv && typeof decEv.data.tier === "number" ? decEv.data.tier : null);
      append(body, el("p", {"class": "muted"}, "Chain data not loaded; from the event feed:"));
      if (tier !== null) { append(body, el("p", null, [tierBadge(tier, tier === 255)])); }
      var mandates = (events || []).filter(function (e) { return e.event.type === "mandate"; });
      mandates.slice(-5).reverse().forEach(function (e) {
        var d = e.event.data || {};
        append(body, el("div", {"class": "small"}, ["mandate ", el("span", {"class": "hash"}, short(d.mandateId)),
          " " + String(d.status || "") + ", total " + usdc(d.maxTotal) + " USDC, per payment " + usdc(d.maxPerTx) + " USDC"]));
      });
      if (tier === null && mandates.length === 0) { append(body, el("p", {"class": "muted"}, "No wallet data yet.")); }
      return;
    }
    var lim = w.limits || {};
    append(body, el("div", null, [tierBadge(w.tier, w.frozen), " ",
      el("span", {"class": "muted small"}, "max auto tier " + String(w.maxAutoTier) +
        (w.frozen ? ", tier before freeze " + String(w.tierBeforeFreeze) : ""))]));
    var spent = Number(w.windowSpent || 0);
    var cap = Number(lim.windowCap || 0);
    var bar = el("progress", {"max": cap > 0 ? String(cap) : "1", "value": String(cap > 0 ? Math.min(spent, cap) : 0)});
    append(body, kv([
      ["Balance", usdc(w.balance) + " USDC"],
      ["Reserve floor", usdc(w.reserveFloor) + " USDC"],
      ["Escrow locked", usdc(w.escrowLocked) + " USDC"],
      ["Free balance", usdc(w.freeBalance) + " USDC"],
      ["Per payment", w.frozen ? "nothing (frozen)" : usdc(lim.perTx) + " USDC (new payees " + usdc(lim.newPayeeCap) + ")"],
      ["24 h window", usdc(w.windowSpent) + " / " + usdc(lim.windowCap) + " USDC, " + String(w.windowCount) + " / " + String(lim.maxTx) + " tx"],
      ["Pay grades", (lim.payGrades || []).join(", ") || "none"],
      ["Escrow grades", (lim.escrowGrades || []).join(", ") || "none"]
    ]));
    append(body, bar);
    var ms = w.mandates || [];
    append(body, el("h3", null, "Mandates (on-chain)"));
    if (ms.length === 0) { append(body, el("p", {"class": "muted small"}, "No mandate registered.")); }
    ms.forEach(function (m) {
      var state = m.active ? "active" : (m.revoked ? "revoked" : "expired");
      var row = el("div", {"class": "mandate"}, [
        el("div", null, [el("span", {"class": "hash", "title": m.mandateId}, short(m.mandateId)), " " + state + " ",
          txLink(m.txHash, m.explorer)]),
        el("div", {"class": "small"}, (m.payees || []).map(function (p) {
          return String(p.name) + " #" + String(p.agentId);
        }).join(", ")),
        el("div", {"class": "small"}, "used " + usdc(m.spent) + " / " + usdc(m.maxTotal) + " USDC, per payment " +
          usdc(m.maxPerTx) + " USDC, until " + chainTime(m.validUntil)),
        el("progress", {"max": String(Math.max(1, Number(m.maxTotal || 0))), "value": String(Number(m.spent || 0))})
      ]);
      if (m.active) {
        var revoke = el("button", {"type": "button", "class": "btn btn-small btn-secondary"}, "Revoke");
        revoke.addEventListener("click", function () { revokeMandate(m.mandateId, row); });
        append(row, el("div", {"class": "row-actions"}, [revoke]));
      }
      append(body, row);
    });
    if (actions) { renderWalletActions(actions, w, snap); }
  }

  function renderWalletActions(actions, w, snap) {
    if (!w.frozen) {
      var freeze = el("button", {"type": "button", "class": "btn btn-danger"}, "Freeze wallet");
      freeze.addEventListener("click", function () {
        if (!root.confirm || root.confirm("Freeze the wallet now (3202)? Only you can unfreeze it.")) {
          ownerAction("/api/freeze", {}, actions);
        }
      });
      append(actions, freeze);
      return;
    }
    var tierSel = el("select", {"name": "toTier", "aria-label": "unfreeze to tier"}, [
      el("option", {"value": "0"}, "Tier 0 (maker-checker)"),
      el("option", {"value": "1"}, "Tier 1 (small payments)")
    ]);
    var rotate = el("input", {"type": "text", "name": "rotateTo", "placeholder": "new agent key address (optional)",
      "maxlength": "42", "autocomplete": "off"});
    if (isAddress(snap && snap.suggestedRotation)) { rotate.value = snap.suggestedRotation; }
    var go = el("button", {"type": "button", "class": "btn"}, "Unfreeze");
    go.addEventListener("click", function () {
      var to = tierSel.value === "1" ? 1 : 0;
      var addr = String(rotate.value || "").trim();
      if (addr && !isAddress(addr)) { showResult(actions, false, "The new agent key must be an address."); return; }
      var question = "Unfreeze to tier " + to + (addr ? " and rotate the agent key to " + addr : " keeping the agent key") + " (3300)?";
      if (!root.confirm || root.confirm(question)) {
        ownerAction("/api/unfreeze", {toTier: to, rotateTo: addr || null}, actions);
      }
    });
    append(actions, [el("div", {"class": "warning"}, "The wallet is frozen. Unfreezing restarts it on probation."),
      el("div", {"class": "inline-form"}, [tierSel, rotate, go])]);
  }

  // ------------------------------------------------------------------ panel 2: grades

  /* agentId -> display name, from the agent's events (it takes names from the pinned directory) */
  function namesFromEvents(events) {
    var names = {};
    (events || []).forEach(function (item) {
      var e = item.event;
      if (!e || e.service !== "agent" || !e.data) { return; }
      [e.data.payee, e.data.subject, e.data.from].forEach(function (p) {
        if (p && typeof p === "object" && p.agentId !== undefined && typeof p.name === "string") {
          names[String(p.agentId)] = p.name;
        }
      });
    });
    return names;
  }

  function renderGrades(snap, events) {
    var body = byId("grades-body");
    if (!body) { return; }
    clear(body);
    var rows = (snap && snap.grades) || null;
    if (!rows) {
      var latest = {};
      (events || []).forEach(function (e) {
        if (e.event.type === "grade" && e.event.data && e.event.data.agentId !== undefined) {
          latest[String(e.event.data.agentId)] = e.event;
        }
      });
      var names = namesFromEvents(events);
      rows = Object.keys(latest).map(function (k) {
        var ev = latest[k];
        var d = ev.data || {};
        return {agentId: k, name: names[k] || ("agent #" + k), grade: d.to, effectiveGrade: d.to, score: d.score,
          reasons: d.reasons || ev.reason_codes || [], txHash: ev.tx_hash, explorer: ev.explorer,
          evidenceUri: d.evidenceUri, previousGrade: d.from, ts: ev.ts};
      });
    }
    if (rows.length === 0) { append(body, el("p", {"class": "muted"}, "No grades yet.")); return; }
    var table = el("table", null, [el("thead", null, el("tr", null, [
      el("th", null, "Agent"), el("th", null, "Grade"), el("th", null, "Score"), el("th", null, "Top reasons"),
      el("th", null, "Last change"), el("th", null, "Evidence")]))]);
    var tbody = el("tbody");
    rows.forEach(function (g) {
      var gradeCell = el("td", null, [gradeBadge(g.grade)]);
      if (g.effectiveGrade && g.effectiveGrade !== g.grade) {
        append(gradeCell, el("div", {"class": "small muted"}, "effective " + String(g.effectiveGrade)));
      }
      if (g.previousGrade && g.previousGrade !== g.grade) {
        append(gradeCell, el("div", {"class": "small muted"}, "was " + String(g.previousGrade)));
      }
      var who = el("td", null, [String(g.name), el("div", {"class": "small muted"}, "#" + String(g.agentId) +
        (g.anchor ? ", anchor" : "") + (g["self"] ? ", our wallet" : ""))]);
      if (g.lookalikeOf) { append(who, el("div", {"class": "flag small"}, "looks like " + String(g.lookalikeOf))); }
      var evidence = el("td", null, [isHash(g.txHash) ? txLink(g.txHash, g.explorer) : "-"]);
      if (safeUrl(g.evidenceUri)) {
        append(evidence, el("div", null, el("a", {"href": g.evidenceUri, "target": "_blank", "rel": "noopener noreferrer"}, "bundle")));
      } else if (g.evidenceUri) {
        append(evidence, el("div", {"class": "small muted"}, String(g.evidenceUri)));
      }
      if (g.recompute) { append(evidence, el("div", {"class": "mono small", "title": "recompute"}, String(g.recompute))); }
      append(tbody, el("tr", null, [who, gradeCell,
        el("td", null, g.score === null || g.score === undefined ? "-" : String(g.score)),
        el("td", null, reasonList((g.reasons || []).slice(0, 4))),
        el("td", {"class": "small"}, g.postedAt ? chainTime(g.postedAt) : (g.ts ? String(g.ts) : "-")),
        evidence]));
    });
    append(table, tbody);
    append(body, table);
  }

  // ------------------------------------------------------------------ panel 3: decision log

  function decisionRows(events) {
    var out = [];
    var byKey = {};
    (events || []).forEach(function (item) {
      var e = item.event;
      if (!e || e.type !== "decision") { return; }
      var d = e.data || {};
      var outcome = String(d.outcome || d.decision || "");
      var key = String(d.orderId || item.id) + "|" + outcome;
      var existing = byKey[key];
      if (existing !== undefined) {
        if (e.service === "agent") { out[existing] = item; }
        return;
      }
      byKey[key] = out.length;
      out.push(item);
    });
    return out.reverse();
  }

  function renderDecisions(events) {
    var body = byId("decisions-body");
    if (!body) { return; }
    clear(body);
    var rows = decisionRows(events);
    if (rows.length === 0) { append(body, el("p", {"class": "muted"}, "No decisions yet.")); return; }
    var table = el("table", null, el("thead", null, el("tr", null, [
      el("th", null, "Time"), el("th", null, "Step"), el("th", null, "Counterparty"), el("th", null, "Amount"),
      el("th", null, "Decision"), el("th", null, "Reasons"), el("th", null, "Tier"), el("th", null, "Tx")])));
    var tbody = el("tbody");
    rows.slice(0, 200).forEach(function (item) {
      var e = item.event;
      var d = e.data || {};
      var payee = d.payee && typeof d.payee === "object" ? d.payee : {};
      var outcome = String(d.outcome || d.decision || "?");
      var cls = /^[A-Z_]+$/.test(outcome) ? "outcome outcome-" + outcome : "outcome";
      var decisionCell = el("td", null, [el("span", {"class": cls}, outcome)]);
      if (d.status) { append(decisionCell, el("div", {"class": "small muted"}, String(d.status))); }
      if (d.fallback) { append(decisionCell, el("div", {"class": "small flag"}, "rule-based mode")); }
      if (d.violation && typeof d.violation === "object") {
        append(decisionCell, el("div", {"class": "small flag"}, "violation " + String(d.violation.kind || "") +
          " (severity " + String(d.violation.severity || "?") + ")"));
      }
      var reasons = el("td", null, [reasonList(e.reason_codes || [])]);
      if (d.ownerReply) { append(reasons, el("div", {"class": "small"}, String(d.ownerReply))); }
      if (d.llmReply) {
        append(reasons, el("div", {"class": "untrusted small"}, [el("span", {"class": "untrusted-label"}, "LLM reply"), String(d.llmReply)]));
      }
      var tx = e.tx_hash ? txLink(e.tx_hash, e.explorer) : el("span", {"class": "muted"}, "off-chain");
      append(tbody, el("tr", null, [
        el("td", {"class": "small"}, eventTime(e.ts)),
        el("td", null, e.step ? String(e.step) : "-"),
        el("td", null, [String(payee.name || "?"), el("div", {"class": "small muted"},
          payee.agentId !== undefined ? "#" + String(payee.agentId) + (payee.grade ? ", grade " + String(payee.grade) : "") : "")]),
        el("td", null, d.amount !== undefined ? usdc(d.amount) : "-"),
        decisionCell,
        reasons,
        el("td", null, d.tier !== undefined && d.tier !== null ? (d.tier === 255 ? "FROZEN" : String(d.tier)) : "-"),
        el("td", null, [tx])]));
    });
    append(table, tbody);
    append(body, table);
  }

  // ------------------------------------------------------------------ panel 4: autonomy timeline

  function renderAutonomy(snap, events) {
    var body = byId("autonomy-body");
    if (!body) { return; }
    clear(body);
    var a = snap && snap.autonomy;
    var list = el("ol", {"class": "timeline"});
    if (a) {
      append(body, el("div", null, [tierBadge(a.tier, a.frozen), " clean settlements " + String(a.cleanSinceChange) +
        (a.cleanNeeded !== null && a.cleanNeeded !== undefined ? " of " + String(a.cleanNeeded) + " for the next tier" : "") +
        ", violation points " + String(a.violationPoints) + ", max auto tier " + String(a.maxAutoTier)]));
      var items = [];
      (a.changes || []).forEach(function (c) {
        items.push({ts: c.timestamp || 0, node: el("li", null, [
          el("strong", null, (c.fromTier === 255 ? "FROZEN" : "Tier " + c.fromTier) + " -> " + (c.toTier === 255 ? "FROZEN" : "Tier " + c.toTier)),
          " " + chainTime(c.timestamp) + " ", txLink(c.txHash, c.explorer), reasonList(c.reasons),
          el("div", {"class": "hash small muted", "title": "evidence hash"}, "evidence " + short(c.evidenceHash))])});
      });
      (a.violations || []).forEach(function (v) {
        items.push({ts: v.timestamp || 0, node: el("li", {"class": "violation"}, [
          el("strong", {"class": "flag"}, "Violation " + String(v.kind) + " (severity " + String(v.severity) + ")"),
          " " + chainTime(v.timestamp) + " ", txLink(v.txHash, v.explorer),
          el("div", {"class": "small muted"}, "tier after: " + (v.tierAfter === 255 ? "FROZEN" : String(v.tierAfter)))])});
      });
      (a.rotations || []).forEach(function (r) {
        items.push({ts: 0, node: el("li", null, ["Agent key rotated to ", el("span", {"class": "addr"}, String(r.newAgent)), " ",
          txLink(r.txHash, r.explorer)])});
      });
      items.sort(function (x, y) { return y.ts - x.ts; });
      items.forEach(function (i) { append(list, i.node); });
    } else {
      (events || []).filter(function (e) { return e.event.type === "tier" || e.event.type === "violation"; })
        .slice().reverse().forEach(function (item) {
          var e = item.event;
          var d = e.data || {};
          var title = e.type === "violation" ? "Violation " + String(d.kind || "?") + " (severity " + String(d.severity || "?") + ")"
            : (d.fromTier !== undefined ? "Tier " + String(d.fromTier) + " -> " : "") + (d.toTier === 255 ? "FROZEN" : "Tier " + String(d.toTier === undefined ? "?" : d.toTier));
          append(list, el("li", e.type === "violation" ? {"class": "violation"} : null, [
            el("strong", e.type === "violation" ? {"class": "flag"} : null, title), " " + eventTime(e.ts) + " ",
            txLink(e.tx_hash, e.explorer), reasonList(e.reason_codes || []),
            d.message ? el("div", {"class": "small muted"}, String(d.message)) : null]));
        });
    }
    if (!list.firstChild) { append(body, el("p", {"class": "muted"}, "No tier changes yet.")); }
    append(body, list);
  }

  // ------------------------------------------------------------------ panel 5: messages

  function renderMessages(snap, events) {
    var body = byId("messages-body");
    if (!body) { return; }
    clear(body);
    var entries = [];
    var seenIds = {};
    var fromSnap = (snap && snap.agent && snap.agent.messages) || [];
    fromSnap.forEach(function (m) {
      seenIds[String(m.messageId)] = true;
      entries.push({kind: "message", from: m.from || {}, text: m.text, flagged: m.flagged, rules: m.detectorRules, when: chainTime(m.receivedAt)});
    });
    (events || []).forEach(function (item) {
      var e = item.event;
      var d = e.data || {};
      if (e.type === "message" && e.service === "agent" && d.from && typeof d.from === "object") {
        if (seenIds[String(d.messageId)]) { return; }
        seenIds[String(d.messageId)] = true;
        entries.push({kind: "message", from: d.from, text: d.text, flagged: d.flagged, rules: d.detectorRules, when: eventTime(e.ts)});
      } else if (e.type === "decision" && d.outcome === "DROPPED") {
        entries.push({kind: "dropped", from: d.payee || {}, text: d.memo, flagged: d.flagged, rules: d.detectorRules,
          when: eventTime(e.ts), codes: e.reason_codes, detail: d.detail, rule: d.rule});
      }
    });
    if (entries.length === 0) { append(body, el("p", {"class": "muted"}, "No counterparty messages.")); return; }
    entries.reverse().slice(0, 100).forEach(function (m) {
      var from = m.from || {};
      var head = el("div", {"class": "small"}, [
        el("strong", null, m.kind === "dropped" ? "Dropped invoice" : "Message"), " from " + String(from.name || "unknown") +
          (from.agentId !== undefined ? " #" + String(from.agentId) : "") + ", " + String(m.when || "")]);
      var box = el("div", {"class": "message"}, [head]);
      if (m.kind === "dropped") {
        append(box, reasonList(m.codes || []));
        if (m.rule || m.detail) { append(box, el("div", {"class": "small muted"}, String(m.rule || "") + " " + String(m.detail || ""))); }
      }
      if (m.flagged) {
        append(box, el("div", {"class": "flag small"}, "detector flags: " + ((m.rules || []).map(String).join(", ") || "yes")));
      }
      append(box, el("div", {"class": "untrusted"}, [el("span", {"class": "untrusted-label"}, "counterparty text (untrusted, shown to you only)"),
        String(m.text === undefined || m.text === null ? "" : m.text)]));
      append(body, box);
    });
  }

  // ------------------------------------------------------------------ chat and cards

  function addChat(role, text, meta) {
    var log = byId("chat-log");
    if (!log) { return null; }
    var bubble = el("div", {"class": "msg msg-" + role}, [
      meta ? el("span", {"class": "msg-meta"}, meta) : null, String(text === undefined || text === null ? "" : text)]);
    append(log, bubble);
    if (typeof log.scrollTop === "number") { log.scrollTop = log.scrollHeight || 0; }
    return bubble;
  }

  function addCard(node) {
    var log = byId("chat-log");
    if (log && node) { append(log, node); if (typeof log.scrollTop === "number") { log.scrollTop = log.scrollHeight || 0; } }
    return node;
  }

  function showResult(container, ok, message, result) {
    var box = el("div", {"class": ok ? "result-ok" : "result-bad"}, String(message));
    if (result && isHash(result.txHash)) { append(box, [" ", txLink(result.txHash, result.explorer)]); }
    if (result && result.agent && typeof result.agent === "object") {
      append(box, el("div", {"class": "small"}, result.agent.notified ? "agent notified" :
        "agent NOT notified: " + String(result.agent.error || "")));
    }
    append(container, box);
    return box;
  }

  function warningsOf(card) {
    var out = [];
    (card.problems || []).forEach(function (p) { out.push(el("div", {"class": "problem"}, "Blocked: " + String(p))); });
    (card.warnings || []).forEach(function (w) { out.push(el("div", {"class": "warning"}, String(w))); });
    return out;
  }

  function payeeTable(rows) {
    var table = el("table", null, el("thead", null, el("tr", null, [el("th", null, "Payee"), el("th", null, "Grade"),
      el("th", null, "Payout wallet (live, pinned on confirm)"), el("th", null, "Registered")])));
    var tbody = el("tbody");
    (rows || []).forEach(function (r) {
      var who = el("td", null, [String(r.name), el("div", {"class": "small muted"}, "#" + String(r.agentId) +
        (r.inDirectory === false ? " NOT IN THE PINNED DIRECTORY" : ""))]);
      if (r.lookalikeOf) { append(who, el("div", {"class": "flag small"}, "looks like " + String(r.lookalikeOf))); }
      append(tbody, el("tr", null, [who,
        el("td", null, [gradeBadge(r.grade), r.score !== null && r.score !== undefined ? " " + String(r.score) : ""]),
        el("td", {"class": "addr"}, r.liveWallet ? String(r.liveWallet) : "none"),
        el("td", null, r.ageS !== null && r.ageS !== undefined ? age(r.ageS) + " ago" : "unknown")]));
    });
    append(table, tbody);
    return table;
  }

  function mandateCard(card) {
    var v = card.values || {};
    var node = el("div", {"class": "card card-mandate", "data-draft": String(card.draftId || "")});
    append(node, [
      el("div", {"class": "card-title"}, "Mandate to register (rebuilt from the chain, not from the agent)"),
      payeeTable(card.payees),
      kv([
        ["Per payment", usdc(v.maxPerTx) + " USDC"],
        ["Total budget", usdc(v.maxTotal) + " USDC"],
        ["Valid until", chainTime(v.validUntil) + " (" + String(card.validHours) + " h)"],
        ["Recurring", v.recurring ? "yes" : "no"],
        ["Purpose", String(card.purpose || "")],
        ["Mandate id", el("span", {"class": "hash"}, String(v.mandateId || ""))],
        ["Code", el("span", {"class": "code"}, String(card.code || ""))]
      ])
    ]);
    append(node, warningsOf(card));
    var confirm = el("button", {"type": "button", "class": "btn"}, "Confirm and register on-chain");
    var cancel = el("button", {"type": "button", "class": "btn btn-secondary"}, "Cancel");
    confirm.disabled = !card.confirmable;
    var buttons = el("div", {"class": "row-actions"}, [confirm, cancel]);
    append(node, [buttons, el("div", {"class": "card-note"},
      "Confirm sends registerMandate with exactly these values from the owner key, then tells the agent.")]);
    confirm.addEventListener("click", function () {
      confirm.disabled = true; cancel.disabled = true;
      api("/api/mandates/" + encodeURIComponent(card.draftId) + "/confirm", {prepareId: card.prepareId})
        .then(function (r) { return finishAction(node, r, "Mandate registered"); })
        .catch(function (err) { showResult(node, false, err.message); });
    });
    cancel.addEventListener("click", function () { confirm.disabled = true; cancel.disabled = true; showResult(node, false, "Cancelled: nothing was signed."); });
    return node;
  }

  function approvalCard(card) {
    var v = card.values || {};
    var inv = card.invoice || {};
    var payee = card.payee || {};
    var node = el("div", {"class": "card card-approval", "data-order": String(card.orderId || "")});
    append(node, [
      el("div", {"class": "card-title"}, "Owner approval needed (invoice signature verified by this console)"),
      payeeTable([payee]),
      kv([
        ["Order", el("span", {"class": "hash"}, String(v.orderId || ""))],
        ["Invoice", String(inv.invoiceId || "") + " (signed by " + String(inv.signer || "") + ")"],
        ["Amount", usdc(v.amount) + " USDC"],
        ["Payment", v.asEscrow ? "escrow until " + chainTime(v.escrowDeadline) : "direct payment"],
        ["Approval valid until", chainTime(v.expiry)],
        ["Code", el("span", {"class": "code"}, String(card.code || ""))]
      ])
    ]);
    var items = el("ul", {"class": "small"});
    (inv.items || []).forEach(function (i) {
      append(items, el("li", null, String(i.sku) + " x" + String(i.quantity) + " at " + usdc(i.unitPrice) + " USDC - " + String(i.description)));
    });
    append(node, items);
    append(node, el("div", {"class": "untrusted"}, [el("span", {"class": "untrusted-label"}, "invoice memo (untrusted)"), String(inv.memo || "")]));
    append(node, warningsOf(card));
    var approve = el("button", {"type": "button", "class": "btn"}, "Approve");
    var reject = el("button", {"type": "button", "class": "btn btn-secondary"}, "Reject");
    append(node, el("div", {"class": "row-actions"}, [approve, reject]));
    approve.addEventListener("click", function () {
      approve.disabled = true; reject.disabled = true;
      api("/api/approvals/" + encodeURIComponent(card.orderId) + "/approve", {prepareId: card.prepareId})
        .then(function (r) { return finishAction(node, r, "Approved"); })
        .catch(function (err) { showResult(node, false, err.message); });
    });
    reject.addEventListener("click", function () {
      approve.disabled = true; reject.disabled = true;
      api("/api/approvals/" + encodeURIComponent(card.orderId) + "/reject", {})
        .then(function (r) { showResult(node, true, "Rejected: nothing was signed.", r); })
        .catch(function (err) { showResult(node, false, err.message); });
    });
    return node;
  }

  function clarificationCard(card) {
    var node = el("div", {"class": "card"}, [
      el("div", {"class": "card-title"}, "The agent asks: " + String(card.question || "")),
      card.candidates && card.candidates.length ? payeeTable(card.candidates) : null,
      el("div", {"class": "card-note"}, "Candidates above were read from the chain and the pinned directory by this console.")
    ]);
    var buttons = el("div", {"class": "row-actions"});
    (card.options || []).forEach(function (opt) {
      var b = el("button", {"type": "button", "class": "btn btn-secondary"}, String(opt));
      b.addEventListener("click", function () { sendChat(String(opt)); });
      append(buttons, b);
    });
    append(node, buttons);
    return node;
  }

  function decisionCard(card) {
    var payee = card.payee || {};
    return el("div", {"class": "card"}, [
      el("div", {"class": "card-title"}, [el("span", {"class": "outcome outcome-" + String(card.outcome).replace(/[^A-Z_]/g, "")}, String(card.outcome)),
        " " + String(payee.name || "?") + (payee.agentId ? " #" + String(payee.agentId) : "") +
        (card.amount ? ", " + usdc(card.amount) + " USDC" : "")]),
      reasonList(card.reasons || []),
      el("div", null, card.offchain ? el("span", {"class": "muted"}, "off-chain") : txLink(card.txHash, card.explorer))
    ]);
  }

  function renderCard(card) {
    if (!card || typeof card !== "object") { return null; }
    switch (card.type) {
      case "mandate": return mandateCard(card);
      case "approval": return approvalCard(card);
      case "clarification": return clarificationCard(card);
      case "decision": return decisionCard(card);
      case "status": return el("div", {"class": "card"}, [el("div", {"class": "card-title"}, "Status (from the chain)"),
        card.wallet ? kv([["Tier", card.wallet.frozen ? "FROZEN" : String(card.wallet.tier)], ["Balance", usdc(card.wallet.balance) + " USDC"],
          ["Free balance", usdc(card.wallet.freeBalance) + " USDC"]]) : null]);
      case "unverified": return el("div", {"class": "card card-bad"}, [el("div", {"class": "card-title"},
        "Not shown: the agent's " + String(card.of || "card") + " could not be verified"), el("div", null, String(card.error || ""))]);
      default: return el("div", {"class": "card card-bad"}, "Unknown card");
    }
  }

  function promptCard(kind, id, label) {
    var key = kind + ":" + id;
    if ((kind === "approval" ? S.shownApprovals : S.shownDrafts)[key]) { return null; }
    (kind === "approval" ? S.shownApprovals : S.shownDrafts)[key] = true;
    var node = el("div", {"class": "card", "data-kind": kind}, [el("div", {"class": "card-title"}, label),
      el("div", {"class": "hash small"}, String(id))]);
    var review = el("button", {"type": "button", "class": "btn"}, "Review (verified by this console)");
    append(node, el("div", {"class": "row-actions"}, [review]));
    review.addEventListener("click", function () {
      review.disabled = true;
      var path = kind === "approval" ? "/api/approvals/" + encodeURIComponent(id) + "/prepare"
        : "/api/mandates/" + encodeURIComponent(id) + "/prepare";
      api(path, {}).then(function (card) {
        card.type = kind === "approval" ? "approval" : "mandate";
        addCard(renderCard(card));
      }).catch(function (err) { showResult(node, false, err.message); review.disabled = false; });
    });
    return addCard(node);
  }

  function finishAction(node, result, label) {
    if (result && result.status === "sign_in_browser") { return browserSign(node, result, label); }
    showResult(node, true, label + ".", result);
    scheduleSnapshot(300);
    return result;
  }

  /* Stretch goal 11.4: OWNER_SIGNING=browser - MetaMask signs exactly the call the server built. */
  function browserSign(node, pending, label) {
    var eth = root.ethereum;
    if (!eth || typeof eth.request !== "function") {
      showResult(node, false, "Browser signing needs MetaMask (window.ethereum).");
      return null;
    }
    var request = pending.kind === "personal_sign"
      ? eth.request({method: "personal_sign", params: [pending.digest, pending.from]}).then(function (sig) { return {signature: sig}; })
      : eth.request({method: "eth_requestAccounts"}).then(function () {
          return eth.request({method: "eth_sendTransaction", params: [pending.tx]});
        }).then(function (hash) { return {txHash: hash}; });
    showResult(node, true, "Confirm in MetaMask...");
    return request.then(function (body) {
      return api("/api/actions/" + encodeURIComponent(pending.actionId) + "/submitted", body);
    }).then(function (r) { showResult(node, true, label + " (signed in the browser).", r); scheduleSnapshot(300); return r; })
      .catch(function (err) { showResult(node, false, String(err && err.message ? err.message : err)); });
  }

  function ownerAction(path, body, container) {
    return api(path, body).then(function (r) { return finishAction(container, r, "Done"); })
      .catch(function (err) { showResult(container, false, err.message); });
  }

  function revokeMandate(mandateId, row) {
    if (root.confirm && !root.confirm("Revoke mandate " + mandateId + " on-chain?")) { return; }
    api("/api/mandates/" + encodeURIComponent(mandateId) + "/revoke", {})
      .then(function (r) { finishAction(row, r, "Mandate revoked"); })
      .catch(function (err) { showResult(row, false, err.message); });
  }

  function sendChat(message) {
    var text = String(message || "").trim();
    if (!text) { return null; }
    addChat("owner", text, "you");
    S.sentMessages.push(text.slice(0, 500));
    var waiting = addChat("system", "the agent is thinking...");
    return api("/api/chat", {message: text}).then(function (r) {
      if (waiting && waiting.parentNode) { waiting.parentNode.removeChild(waiting); }
      handleChatResponse(r);
    }).catch(function (err) {
      if (waiting && waiting.parentNode) { waiting.parentNode.removeChild(waiting); }
      addChat("system", "error: " + err.message);
    });
  }

  function handleChatResponse(r) {
    var llm = r.llm || {};
    S.llm = llm.mode ? llm : S.llm;
    S.fallback = !!llm.fallback;
    addChat("agent", r.reply || "", "agent" + (llm.fallback ? " (rule-based mode)" : ""));
    (r.cards || []).forEach(function (card) {
      if (card.type === "mandate" && card.draftId) { S.shownDrafts["draft:" + card.draftId] = true; }
      if (card.type === "approval" && card.orderId) { S.shownApprovals["approval:" + card.orderId] = true; }
      addCard(renderCard(card));
    });
    renderBanner(S.snapshot);
  }

  // ------------------------------------------------------------------ live events

  function handleItem(item) {
    if (!item || typeof item !== "object" || typeof item.id !== "number" || !item.event) { return false; }
    if (S.seen[item.id]) { return false; }
    S.seen[item.id] = true;
    S.events.push(item);
    if (S.events.length > 3000) { S.events.splice(0, S.events.length - 3000); }
    var e = item.event;
    var d = e.data || {};
    if (e.type === "decision") {
      if (typeof d.fallback === "boolean" && e.service === "agent") { S.fallback = d.fallback; }
      renderDecisions(S.events);
      renderMessages(S.snapshot, S.events);
    } else if (e.type === "message") {
      renderMessages(S.snapshot, S.events);
    } else if (e.type === "llm_switch" && d.to && typeof d.to === "object") {
      S.llm = d.to;
    } else if (e.type === "approval" && d.status === "requested" && isHash(d.orderId) && e.service === "agent") {
      promptCard("approval", d.orderId, "Approval requested by the agent for " + String((d.payee && d.payee.name) || "a payee") +
        ", " + usdc(d.amount) + " USDC");
    } else if (e.type === "card" && d.type === "mandate_draft" && typeof d.draftId === "string" && e.service === "agent") {
      promptCard("draft", d.draftId, "Mandate draft from the agent");
    } else if (e.type === "chat" && e.service === "agent") {
      var idx = S.sentMessages.indexOf(String(d.message || ""));
      if (idx >= 0) { S.sentMessages.splice(idx, 1); }
      else {
        addChat("owner", d.message, "owner (another client)");
        addChat("agent", d.reply, "agent");
      }
    }
    if (/^(tx|tier|grade|mandate|violation|approval|decision|report)$/.test(e.type)) { scheduleSnapshot(800); }
    renderBanner(S.snapshot);
    return true;
  }

  /* approval requests of the event feed that no later approval event closed */
  function openApprovals(events) {
    var open = {};
    var order = [];
    (events || []).forEach(function (item) {
      var e = item.event;
      if (!e || e.type !== "approval" || !e.data || !isHash(e.data.orderId)) { return; }
      var id = e.data.orderId.toLowerCase();
      if (e.data.status === "requested" && e.service === "agent") {
        if (!open[id]) { order.push(id); }
        open[id] = {orderId: id, name: e.data.payee && e.data.payee.name, amount: e.data.amount};
      } else {
        open[id] = null;
      }
    });
    return order.filter(function (id) { return open[id]; }).map(function (id) { return open[id]; });
  }

  function renderAll(snap, items) {
    S.snapshot = snap || null;
    S.reasonTexts = (snap && snap.reasonTexts) || S.reasonTexts || {};
    S.explorerTx = (snap && snap.network && snap.network.explorerTx) || null;
    (items || []).forEach(function (item) {
      if (item && typeof item.id === "number" && !S.seen[item.id]) { S.seen[item.id] = true; S.events.push(item); }
    });
    renderBanner(S.snapshot);
    renderWallet(S.snapshot, S.events);
    renderGrades(S.snapshot, S.events);
    renderDecisions(S.events);
    renderAutonomy(S.snapshot, S.events);
    renderMessages(S.snapshot, S.events);
    openApprovals(S.events).forEach(function (a) {
      promptCard("approval", a.orderId, "Approval requested by the agent for " + String(a.name || "a payee") +
        ", " + usdc(a.amount) + " USDC");
    });
    if (snap && snap.agent && Array.isArray(snap.agent.approvals)) {
      snap.agent.approvals.forEach(function (a) {
        if (isHash(a.orderId)) { promptCard("approval", a.orderId, "Approval waiting for you"); }
      });
    }
  }

  function loadSnapshot() {
    return getJson("/api/snapshot").then(function (snap) { renderAll(snap, snap.events || []); })
      .catch(function (err) { addChat("system", "could not load the panels: " + err.message); });
  }

  function scheduleSnapshot(delay) {
    if (S.snapshotTimer) { return; }
    S.snapshotTimer = root.setTimeout(function () { S.snapshotTimer = null; loadSnapshot(); }, delay);
  }

  function startStream() {
    if (typeof root.EventSource !== "function") { return null; }
    var es = new root.EventSource("/stream");
    es.addEventListener("message", function (m) {
      try { handleItem(JSON.parse(m.data)); } catch (err) { /* a malformed event is ignored */ }
    });
    return es;
  }

  // ------------------------------------------------------------------ start

  function start() {
    var meta = doc.querySelector ? doc.querySelector("meta[name=acb-csrf]") : null;
    S.csrf = meta ? String(meta.getAttribute("content") || "") : "";
    var login = byId("login");
    var main = byId("main");
    var logout = byId("logout");
    if (!S.csrf) {
      login.hidden = false;
      main.hidden = true;
      byId("login-form").addEventListener("submit", function (ev) {
        ev.preventDefault();
        var pw = byId("login-password").value;
        root.fetch("/login", {method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({password: pw})}).then(function (r) {
          if (r.ok) { root.location.reload(); return; }
          return r.json().catch(function () { return {}; }).then(function (d) {
            byId("login-error").textContent = String(d && d.detail ? d.detail : "sign-in failed");
          });
        });
      });
      return;
    }
    login.hidden = true;
    main.hidden = false;
    logout.hidden = false;
    logout.addEventListener("click", function () {
      api("/logout", {}).then(function () { root.location.reload(); }).catch(function () { root.location.reload(); });
    });
    byId("chat-form").addEventListener("submit", function (ev) {
      ev.preventDefault();
      var input = byId("chat-input");
      var text = input.value;
      input.value = "";
      sendChat(text);
    });
    loadSnapshot().then(startStream);
  }

  root.ACB_OWNER = {
    renderAll: renderAll, renderBanner: renderBanner, renderWallet: renderWallet, renderGrades: renderGrades,
    renderDecisions: renderDecisions, renderAutonomy: renderAutonomy, renderMessages: renderMessages,
    renderCard: renderCard, handleItem: handleItem, handleChatResponse: handleChatResponse, addChat: addChat,
    usdc: usdc, safeUrl: safeUrl, state: S
  };

  if (!root.ACB_TEST) {
    if (doc.readyState === "loading") { doc.addEventListener("DOMContentLoaded", start); } else { start(); }
  }
}());
