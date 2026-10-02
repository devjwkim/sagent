// sagent visual loop editor. Data comes from <script type="application/json" id="loop-data">;
// everything user-provided is rendered with textContent / setAttribute (never innerHTML).
(function () {
  "use strict";
  var root = document.getElementById("loop-editor");
  if (!root) return;
  var SVGNS = "http://www.w3.org/2000/svg";
  var NODE_W = 164, NODE_H = 48;
  var CONDITIONS = ["always", "success", "failure", "approve", "reject"];
  var data = JSON.parse(document.getElementById("loop-data").textContent);
  var loop = data.loop;
  var state = { selected: null, connectFrom: null, dirty: false };
  var svg = root.querySelector("svg.editor-canvas");
  var panel = root.querySelector(".editor-panel");
  var status = root.querySelector(".editor-status");

  function el(tag, attrs, text) {
    var e = document.createElementNS(SVGNS, tag);
    Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    if (text != null) e.textContent = text;
    return e;
  }
  function h(tag, attrs, text) {
    var e = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "class") e.className = attrs[k]; else e.setAttribute(k, attrs[k]);
    });
    if (text != null) e.textContent = text;
    return e;
  }
  function markDirty() { state.dirty = true; status.textContent = "저장되지 않은 변경이 있습니다."; }

  function ensurePositions() {
    var keys = Object.keys(loop.nodes);
    keys.forEach(function (k, i) {
      var n = loop.nodes[k];
      if (!n.ui) n.ui = data.positions[k] || { x: 20 + (i % 5) * 222, y: 20 + Math.floor(i / 5) * 96 };
    });
  }

  function edgePath(a, b) {
    var sx = a.ui.x + NODE_W, sy = a.ui.y + NODE_H / 2, tx = b.ui.x, ty = b.ui.y + NODE_H / 2;
    if (tx > sx - NODE_W / 2) {
      var mx = (sx + tx) / 2;
      return { d: "M" + sx + "," + sy + " C" + mx + "," + sy + " " + mx + "," + ty + " " + (tx - 4) + "," + ty,
               lx: mx, ly: Math.min(sy, ty) - 7, back: false };
    }
    var x1 = a.ui.x + NODE_W / 2 + 10, y1 = a.ui.y + NODE_H, x2 = b.ui.x + NODE_W / 2 - 10, y2 = b.ui.y + NODE_H;
    var depth = 60;
    return { d: "M" + x1 + "," + y1 + " C" + x1 + "," + (y1 + depth) + " " + x2 + "," + (y2 + depth) + " " + x2 + "," + (y2 + 4),
             lx: (x1 + x2) / 2, ly: Math.max(y1, y2) + depth * 0.75, back: true };
  }

  function render() {
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    var defs = el("defs");
    var marker = el("marker", { id: "ed-arrow", viewBox: "0 0 10 10", refX: "8", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" });
    marker.appendChild(el("path", { d: "M0,0 L10,5 L0,10 z", "class": "arrowhead" }));
    defs.appendChild(marker);
    svg.appendChild(defs);
    var maxX = 600, maxY = 300;
    loop.edges.forEach(function (e, i) {
      var a = loop.nodes[e.from], b = loop.nodes[e.to];
      if (!a || !b) return;
      var p = edgePath(a, b);
      var when = e.when || "always";
      var path = el("path", { d: p.d, "class": "edge edge-" + when + (p.back ? " back" : ""), "marker-end": "url(#ed-arrow)" });
      svg.appendChild(path);
      if (when !== "always") svg.appendChild(el("text", { x: p.lx, y: p.ly, "class": "edge-label edge-label-" + when, "text-anchor": "middle" }, when));
    });
    Object.keys(loop.nodes).forEach(function (key) {
      var n = loop.nodes[key];
      maxX = Math.max(maxX, n.ui.x + NODE_W + 40);
      maxY = Math.max(maxY, n.ui.y + NODE_H + 120);
      var g = el("g", { "class": "node node-" + n.type + (state.selected === key ? " selected" : "") +
                         (state.connectFrom === key ? " connecting" : "") + (loop.start === key ? " start" : ""),
                         "data-key": key });
      g.appendChild(el("rect", { x: n.ui.x, y: n.ui.y, width: NODE_W, height: NODE_H, rx: 8 }));
      g.appendChild(el("text", { x: n.ui.x + 10, y: n.ui.y + 20, "class": "node-title" }, key));
      var sub = n.type + (n.suite ? " · " + n.suite : "") + (n.provider ? " · " + n.provider : "") +
                (n.prompt_ref ? " · " + n.prompt_ref : "");
      g.appendChild(el("text", { x: n.ui.x + 10, y: n.ui.y + 37, "class": "node-sub" }, sub));
      if (loop.start === key) g.appendChild(el("text", { x: n.ui.x + NODE_W - 8, y: n.ui.y + 20, "class": "node-status", "text-anchor": "end" }, "START"));
      svg.appendChild(g);
    });
    // keep a stable canvas while dragging; grow only, so the scale does not jump
    state.vbW = Math.max(state.vbW || 0, maxX);
    state.vbH = Math.max(state.vbH || 0, maxY);
    svg.setAttribute("viewBox", "0 0 " + state.vbW + " " + state.vbH);
    renderPanel();
  }

  // --- dragging & selection ---
  var drag = null;
  function svgPoint(ev) {
    var pt = svg.createSVGPoint();
    pt.x = ev.clientX; pt.y = ev.clientY;
    return pt.matrixTransform(svg.getScreenCTM().inverse());
  }
  svg.addEventListener("pointerdown", function (ev) {
    var g = ev.target.closest("g.node");
    if (!g) { state.selected = null; state.connectFrom = null; render(); return; }
    var key = g.getAttribute("data-key");
    if (state.connectFrom && state.connectFrom !== key) {
      loop.edges.push({ from: state.connectFrom, to: key, when: "always" });
      state.connectFrom = null; state.selected = key; markDirty(); render(); return;
    }
    var p = svgPoint(ev), n = loop.nodes[key];
    drag = { key: key, dx: p.x - n.ui.x, dy: p.y - n.ui.y, moved: false };
    svg.setPointerCapture(ev.pointerId);
    if (state.selected !== key) { state.selected = key; render(); }
  });
  svg.addEventListener("pointermove", function (ev) {
    if (!drag) return;
    var p = svgPoint(ev), n = loop.nodes[drag.key];
    n.ui.x = Math.max(0, Math.round((p.x - drag.dx) / 4) * 4);
    n.ui.y = Math.max(0, Math.round((p.y - drag.dy) / 4) * 4);
    drag.moved = true;
    render();
  });
  svg.addEventListener("pointerup", function () { if (drag && drag.moved) markDirty(); drag = null; });

  // --- side panel ---
  function field(label, input) { var l = h("label", {}, label); l.appendChild(input); return l; }
  function select(options, value, onchange) {
    var s = h("select");
    options.forEach(function (o) {
      var opt = h("option", { value: o[0] }, o[1]);
      if (o[0] === (value || "")) opt.selected = true;
      s.appendChild(opt);
    });
    s.addEventListener("change", function () { onchange(s.value); markDirty(); render(); });
    return s;
  }
  function input(value, onchange, attrs) {
    var i = h("input", attrs || {});
    i.value = value == null ? "" : value;
    i.addEventListener("change", function () { onchange(i.value); markDirty(); render(); });
    return i;
  }

  function renderPanel() {
    while (panel.firstChild) panel.removeChild(panel.firstChild);
    var top = h("div", { "class": "stack" });
    top.appendChild(h("h3", {}, "Loop"));
    top.appendChild(field("이름", input(data.name, function (v) { data.name = v.trim(); }, { maxlength: "40" })));
    top.appendChild(field("설명", input(loop.description, function (v) { loop.description = v; }, { maxlength: "200" })));
    top.appendChild(field("최대 반복", input(loop.max_iterations || 5, function (v) { loop.max_iterations = parseInt(v, 10) || 5; }, { type: "number", min: "1", max: "50" })));
    var keys = Object.keys(loop.nodes);
    top.appendChild(field("시작 노드", select(keys.map(function (k) { return [k, k]; }), loop.start, function (v) { loop.start = v; })));
    panel.appendChild(top);

    var add = h("div", { "class": "row gap wrap editor-add" });
    ["agent", "test", "review", "end"].forEach(function (t) {
      var b = h("button", { type: "button", "class": "small" }, "+ " + t);
      b.addEventListener("click", function () {
        var base = t === "agent" ? "step" : t, i = 1, key = base;
        while (loop.nodes[key]) { i += 1; key = base + i; }
        var node = { type: t, ui: { x: 20, y: 20 + Object.keys(loop.nodes).length * 12 } };
        if (t === "agent") node.prompt = "{task}\n\n{failure}{review}";
        if (t === "test") node.suite = "unit";
        loop.nodes[key] = node;
        state.selected = key; markDirty(); render();
      });
      add.appendChild(b);
    });
    panel.appendChild(add);

    var key = state.selected;
    if (key && loop.nodes[key]) {
      var n = loop.nodes[key];
      var box = h("div", { "class": "stack editor-node" });
      box.appendChild(h("h3", {}, "노드 " + key));
      box.appendChild(field("이름", input(key, function (v) {
        v = v.trim().toLowerCase();
        if (!v || v === key || loop.nodes[v] || !/^[a-z0-9][a-z0-9_-]{0,39}$/.test(v)) return;
        loop.nodes[v] = n; delete loop.nodes[key];
        loop.edges.forEach(function (e) { if (e.from === key) e.from = v; if (e.to === key) e.to = v; });
        if (loop.start === key) loop.start = v;
        state.selected = v;
      }, { maxlength: "40" })));
      if (n.type === "agent") {
        box.appendChild(field("에이전트", select([["", "Harness 설정"], ["claude", "claude"], ["codex", "codex"]], n.provider, function (v) { n.provider = v || undefined; })));
        box.appendChild(field("역할 (harness agents.*)", select([["coding", "coding"], ["review", "review"], ["test_analysis", "test_analysis"]], n.role || "coding", function (v) { n.role = v; })));
        box.appendChild(field("프롬프트 템플릿 (name 또는 name@v)", input(n.prompt_ref, function (v) { n.prompt_ref = v.trim() || undefined; }, { maxlength: "60" })));
        var ta = h("textarea", { rows: "6", "class": "code-editor" });
        ta.value = n.prompt || "";
        ta.addEventListener("change", function () { n.prompt = ta.value; markDirty(); });
        box.appendChild(field("프롬프트 ({task} {plan} {failure} {review})", ta));
      }
      if (n.type === "test") {
        box.appendChild(field("스위트", select([["unit", "unit"], ["lint", "lint"], ["typecheck", "typecheck"], ["e2e", "e2e"]], n.suite, function (v) { n.suite = v; })));
      }
      if (n.type === "review") {
        box.appendChild(field("리뷰 에이전트", select([["", "review.yaml 설정"], ["claude", "claude"], ["codex", "codex"]], n.provider, function (v) { n.provider = v || undefined; })));
      }
      if (n.type === "end") {
        var lc = [["", "변경 안 함"]].concat(data.lifecycles.map(function (l) { return [l, l]; }));
        box.appendChild(field("완료 시 프로젝트 Lifecycle", select(lc, n.lifecycle, function (v) { n.lifecycle = v || undefined; })));
      }
      if (n.type !== "end") {
        box.appendChild(field("재시도 횟수", input(n.retries || 0, function (v) { n.retries = parseInt(v, 10) || 0; }, { type: "number", min: "0", max: "10" })));
        box.appendChild(field("타임아웃(초, 0=없음)", input(n.timeout_sec || 0, function (v) { n.timeout_sec = parseInt(v, 10) || 0; }, { type: "number", min: "0" })));
      }
      var actions = h("div", { "class": "row gap wrap" });
      var connect = h("button", { type: "button", "class": "small primary" }, state.connectFrom === key ? "대상 노드를 클릭…" : "연결 시작");
      connect.addEventListener("click", function () { state.connectFrom = state.connectFrom === key ? null : key; render(); });
      var del = h("button", { type: "button", "class": "small" }, "노드 삭제");
      del.addEventListener("click", function () {
        delete loop.nodes[key];
        loop.edges = loop.edges.filter(function (e) { return e.from !== key && e.to !== key; });
        if (loop.start === key) loop.start = Object.keys(loop.nodes)[0];
        state.selected = null; markDirty(); render();
      });
      actions.appendChild(connect); actions.appendChild(del);
      box.appendChild(actions);

      var out = loop.edges.filter(function (e) { return e.from === key; });
      if (out.length) {
        box.appendChild(h("h4", {}, "나가는 연결"));
        out.forEach(function (e) {
          var row = h("div", { "class": "row gap" });
          row.appendChild(h("span", { "class": "mono small" }, "→ " + e.to));
          row.appendChild(select(CONDITIONS.map(function (c) { return [c, c]; }), e.when || "always", function (v) { e.when = v; }));
          var x = h("button", { type: "button", "class": "small" }, "삭제");
          x.addEventListener("click", function () { loop.edges.splice(loop.edges.indexOf(e), 1); markDirty(); render(); });
          row.appendChild(x);
          box.appendChild(row);
        });
      }
      panel.appendChild(box);
    } else {
      panel.appendChild(h("p", { "class": "muted small" }, "노드를 클릭해 편집하고, 드래그해 위치를 옮기세요. ‘연결 시작’ 후 대상 노드를 클릭하면 연결됩니다."));
    }
  }

  root.querySelector(".editor-save").addEventListener("click", function () {
    var csrf = document.querySelector("meta[name=csrf-token]").getAttribute("content");
    status.textContent = "저장 중…";
    fetch(root.getAttribute("data-save-url"), {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify({ name: data.name, old_name: data.old_name, loop: loop, make_default: root.querySelector(".editor-default").checked }),
      credentials: "same-origin"
    }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
      .then(function (res) {
        if (res.ok) {
          state.dirty = false; data.old_name = data.name;
          status.textContent = "저장했습니다.";
          if (res.body.redirect) window.location.assign(res.body.redirect);
        } else {
          status.textContent = "저장 실패: " + (res.body.error || "알 수 없는 오류");
        }
      })
      .catch(function () { status.textContent = "저장 실패: 네트워크 오류"; });
  });
  window.addEventListener("beforeunload", function (ev) { if (state.dirty) { ev.preventDefault(); ev.returnValue = ""; } });

  ensurePositions();
  render();
})();
