// Interactive terminal: xterm.js <-> WebSocket <-> `tmux attach` on the server.
(function () {
  "use strict";
  var box = document.getElementById("xterm");
  if (!box || !window.Terminal) return;
  var openBtn = document.getElementById("xterm-open");
  var closeBtn = document.getElementById("xterm-close");
  var info = document.getElementById("xterm-status");
  var term = null, sock = null, fit = null;

  function setStatus(t) { info.textContent = t; }

  function connect() {
    box.hidden = false; openBtn.hidden = true; closeBtn.hidden = false;
    var readOnly = box.getAttribute("data-readonly") === "1";
    term = new window.Terminal({ cursorBlink: !readOnly, disableStdin: readOnly, fontSize: 13,
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace", scrollback: 5000,
      theme: { background: "#0d1117" } });
    fit = new window.FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(box);
    fit.fit();
    var proto = window.location.protocol === "https:" ? "wss://" : "ws://";
    // url_for() may render an absolute ws:// URL; always reconnect to the page's own host.
    var path = new URL(box.getAttribute("data-ws"), window.location.href).pathname;
    sock = new WebSocket(proto + window.location.host + path);
    sock.onopen = function () {
      setStatus(readOnly ? "연결됨 (읽기 전용)" : "연결됨 — 입력이 에이전트 터미널로 전달됩니다");
      sock.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
      term.focus();
    };
    sock.onmessage = function (ev) { term.write(ev.data); };
    sock.onclose = function () { setStatus("연결 종료"); };
    sock.onerror = function () { setStatus("연결 오류"); };
    term.onData(function (d) {
      if (!readOnly && sock.readyState === 1) sock.send(JSON.stringify({ type: "input", data: d }));
    });
    window.addEventListener("resize", onResize);
  }

  function onResize() {
    if (!term || !fit) return;
    fit.fit();
    if (sock && sock.readyState === 1) sock.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
  }

  function disconnect() {
    window.removeEventListener("resize", onResize);
    if (sock) sock.close();
    if (term) term.dispose();
    term = sock = fit = null;
    box.hidden = true; openBtn.hidden = false; closeBtn.hidden = true;
    setStatus("");
  }

  openBtn.addEventListener("click", connect);
  closeBtn.addEventListener("click", disconnect);
  if (box.getAttribute("data-autostart") === "1") connect();
})();
