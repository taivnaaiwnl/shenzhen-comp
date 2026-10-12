/* Read-only grade lookup (spec 11.4): reads AgentCreditBureau over JSON-RPC in the browser.
 * It only uses eth_call, eth_blockNumber and eth_getLogs; it never signs or sends anything.
 * Every value read from the chain (names, URIs) is shown with textContent only. */
(function () {
  "use strict";

  var SEL_GET_RECORD = "0x03e9e609";      // getRecord(uint256)
  var SEL_EFFECTIVE = "0x6d1273a8";       // effectiveGrade(uint256,uint64)
  var SEL_TOKEN_URI = "0xc87b56dd";       // tokenURI(uint256)
  var TOPIC_GRADE_POSTED = "0x9930d87e432d5b546ff43fc8b779e49afb12b3449c7c3ae9c871a08db476a641";
  var GRADES = ["NONE", "NEW", "D", "C", "B", "A"];
  var LOG_CHUNK = 200;                    // the public Base Sepolia RPC limits eth_getLogs to 200 blocks
  var REASONS = {
    1001: "Share of orders delivered on time is low",
    1002: "Orders were paid but not delivered",
    1003: "Trading partners in good standing reported bad data, or the agent clawed back delivered orders",
    1004: "Signed content that tried to manipulate a paying agent",
    1005: "Signed unsolicited invoices",
    1006: "Few completed orders",
    1007: "Identity registered recently",
    1008: "Trades with few distinct, well-graded partners",
    1009: "Most volume is with poorly graded or new partners",
    1010: "Volume flows in a circle back to the agent (wash trading)",
    1011: "Recent order sizes are far above its usual size",
    1012: "No completed trade with a partner in good standing",
    1100: "No adverse factors",
    1101: "Not enough history yet: treated as new",
    1102: "Grade revised after an upheld appeal",
    1103: "Appeal reviewed and rejected",
    1104: "Grade set by a documented manual override"
  };
  var EXPLORERS = {base_sepolia: "https://sepolia.basescan.org/tx/"};

  function $(id) { return document.getElementById(id); }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.setAttribute("class", cls); }
    if (text !== undefined && text !== null) { n.textContent = String(text); }
    return n;
  }

  function word(n) {
    var hex = BigInt(n).toString(16);
    if (hex.length > 64) { throw new Error("number too large"); }
    return "0".repeat(64 - hex.length) + hex;
  }

  function isAddress(a) { return /^0x[0-9a-fA-F]{40}$/.test(a); }

  var rpcId = 0;
  function rpc(url, method, params) {
    rpcId += 1;
    return fetch(url, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({jsonrpc: "2.0", id: rpcId, method: method, params: params})
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d.error) { throw new Error(method + ": " + String(d.error.message || "error")); }
      return d.result;
    });
  }

  function call(url, to, data) { return rpc(url, "eth_call", [{to: to, data: data}, "latest"]); }

  function words(hex) {
    var body = String(hex || "").replace(/^0x/, "");
    var out = [];
    for (var i = 0; i + 64 <= body.length; i += 64) { out.push(body.slice(i, i + 64)); }
    return out;
  }

  function big(w) { return BigInt("0x" + (w || "0")); }

  function decodeString(hex, offsetWordIndex) {
    var body = String(hex || "").replace(/^0x/, "");
    var w = words(hex);
    var offset = Number(big(w[offsetWordIndex]));
    var len = Number(BigInt("0x" + body.slice(offset * 2, offset * 2 + 64)));
    var bytesHex = body.slice(offset * 2 + 64, offset * 2 + 64 + len * 2);
    var bytes = new Uint8Array(len);
    for (var i = 0; i < len; i++) { bytes[i] = parseInt(bytesHex.slice(i * 2, i * 2 + 2), 16); }
    return new TextDecoder("utf-8", {fatal: false}).decode(bytes);
  }

  function gradeName(n) { return GRADES[Number(n)] || "?"; }

  function badge(name) {
    var g = /^(A|B|C|D|NEW|NONE)$/.test(name) ? name : "NONE";
    return el("span", "grade grade-" + g, g);
  }

  function nameFromUri(uri) {
    var m = /^data:application\/json;base64,([A-Za-z0-9+\/=]+)$/.exec(uri || "");
    if (!m) { return null; }
    try {
      var reg = JSON.parse(atob(m[1]));
      return typeof reg.name === "string" ? reg.name.slice(0, 80) : null;
    } catch (e) { return null; }
  }

  function findPosting(url, acb, id, scan) {
    var topic = "0x" + word(id);
    return rpc(url, "eth_blockNumber", []).then(function (latestHex) {
      var latest = Number(BigInt(latestHex));
      var stop = Math.max(0, latest - scan);
      function step(to) {
        if (to < stop) { return null; }
        var from = Math.max(stop, to - LOG_CHUNK + 1);
        return rpc(url, "eth_getLogs", [{address: acb, topics: [TOPIC_GRADE_POSTED, topic],
          fromBlock: "0x" + from.toString(16), toBlock: "0x" + to.toString(16)}]).then(function (logs) {
          if (logs && logs.length) { return logs[logs.length - 1]; }
          return step(from - 1);
        });
      }
      return step(latest);
    });
  }

  function row(url, acb, identity, id, network, scan) {
    var tr = document.createElement("tr");
    var data = SEL_GET_RECORD + word(id);
    return Promise.all([
      call(url, acb, data),
      call(url, acb, SEL_EFFECTIVE + word(id) + word(0)),
      isAddress(identity) ? call(url, identity, SEL_TOKEN_URI + word(id)).catch(function () { return null; }) : Promise.resolve(null)
    ]).then(function (res) {
      var w = words(res[0]);
      var grade = gradeName(big(w[0]));
      var prev = gradeName(big(w[1]));
      var score = Number(big(w[2]));
      var postedAt = Number(big(w[3]));
      var packed = big(w[6]);
      var reasons = [];
      for (var i = 0; i < 4; i++) {
        var code = Number((packed >> BigInt(16 * i)) & BigInt(0xffff));
        if (code) { reasons.push(code); }
      }
      var effective = gradeName(big(words(res[1])[0]));
      var name = res[2] ? nameFromUri(decodeString(res[2], 0)) : null;
      var who = el("td", null, "#" + String(id));
      if (name) { who.appendChild(el("div", "muted", name + " (registered name, unverified)")); }
      tr.appendChild(who);
      var g = el("td"); g.appendChild(badge(grade));
      if (prev !== "NONE" && prev !== grade) { g.appendChild(el("div", "muted", "was " + prev)); }
      tr.appendChild(g);
      var e = el("td"); e.appendChild(badge(effective)); tr.appendChild(e);
      tr.appendChild(el("td", null, grade === "NONE" ? "-" : String(score)));
      var rs = el("td");
      reasons.forEach(function (c) { rs.appendChild(el("div", null, String(c) + ": " + (REASONS[c] || "see the reason catalogue"))); });
      tr.appendChild(rs);
      tr.appendChild(el("td", null, postedAt ? new Date(postedAt * 1000).toISOString() : "-"));
      var ev = el("td");
      ev.appendChild(el("div", "mono", "evidence " + "0x" + w[10]));
      tr.appendChild(ev);
      if (grade === "NONE" || scan <= 0) { return tr; }
      return findPosting(url, acb, id, scan).then(function (log) {
        if (!log) { ev.appendChild(el("div", "muted", "posting not found in the scanned blocks")); return tr; }
        var explorer = EXPLORERS[network];
        if (explorer && /^0x[0-9a-f]{64}$/.test(log.transactionHash)) {
          var a = el("a", null, "posting tx");
          a.setAttribute("href", explorer + log.transactionHash);
          a.setAttribute("rel", "noopener noreferrer");
          ev.appendChild(a);
        } else {
          ev.appendChild(el("div", "mono", log.transactionHash));
        }
        var uri = decodeString(log.data, 10);
        if (/^https:\/\//.test(uri)) {
          var b = el("a", null, " evidence bundle");
          b.setAttribute("href", uri.slice(0, 500));
          b.setAttribute("rel", "noopener noreferrer");
          ev.appendChild(b);
        } else if (uri) {
          ev.appendChild(el("div", "muted", uri.slice(0, 300)));
        }
        ev.appendChild(el("div", "mono", "acb bureau recompute --network " + network + " --tx " + log.transactionHash));
        return tr;
      });
    });
  }

  function lookup(ev) {
    ev.preventDefault();
    var url = $("rpc").value.trim();
    var acb = $("acb").value.trim();
    var identity = $("identity").value.trim();
    var network = $("network").value.trim().replace(/[^a-z0-9_]/gi, "") || "base_sepolia";
    var scan = Math.max(0, Math.min(200000, parseInt($("scan").value, 10) || 0));
    var ids = $("ids").value.split(",").map(function (s) { return s.trim(); }).filter(function (s) { return /^[0-9]{1,30}$/.test(s); });
    var status = $("status");
    var rows = $("rows");
    status.className = "status";
    if (!/^https?:\/\//.test(url)) { status.textContent = "Enter an http(s) RPC URL."; return; }
    if (!isAddress(acb)) { status.textContent = "Enter the AgentCreditBureau address."; return; }
    if (!ids.length) { status.textContent = "Enter at least one agent id."; return; }
    while (rows.firstChild) { rows.removeChild(rows.firstChild); }
    status.textContent = "Reading the chain...";
    var chain = Promise.resolve();
    ids.forEach(function (id) {
      chain = chain.then(function () { return row(url, acb, identity, id, network, scan); })
        .then(function (tr) { rows.appendChild(tr); })
        .catch(function (err) {
          var tr = document.createElement("tr");
          tr.appendChild(el("td", null, "#" + id));
          tr.appendChild(el("td", "error", String(err && err.message ? err.message : err)));
          rows.appendChild(tr);
        });
    });
    chain.then(function () { status.textContent = "Done."; $("results").hidden = false; });
  }

  function init() {
    var params = new URLSearchParams(window.location.search);
    ["rpc", "acb", "identity", "ids", "network"].forEach(function (k) {
      var v = params.get(k);
      if (v && v.length < 500) { $(k).value = v; }
    });
    $("lookup").addEventListener("submit", lookup);
  }

  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", init); } else { init(); }
}());
