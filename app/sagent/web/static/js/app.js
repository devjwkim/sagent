// sagent UI helpers. No inline scripts are allowed by the CSP, so behaviour is
// attached through data-* attributes here.
(function () {
  "use strict";

  document.addEventListener("submit", function (ev) {
    var form = ev.target.closest("form[data-confirm]");
    if (form && !window.confirm(form.getAttribute("data-confirm"))) {
      ev.preventDefault();
      ev.stopImmediatePropagation();
    }
  }, true);

  document.addEventListener("change", function (ev) {
    var el = ev.target;
    if (el.matches && el.matches("select[data-autosubmit]") && el.form) {
      el.form.requestSubmit();
    }
  });

  // Keep terminals pinned to the bottom unless the user scrolled up.
  document.addEventListener("htmx:beforeSwap", function (ev) {
    var el = ev.detail.target;
    if (el && el.hasAttribute && el.hasAttribute("data-autoscroll")) {
      el.dataset.pinned = (el.scrollHeight - el.scrollTop - el.clientHeight < 40) ? "1" : "0";
    }
  });
  document.addEventListener("htmx:afterSwap", function (ev) {
    var el = ev.detail.target;
    if (el && el.hasAttribute && el.hasAttribute("data-autoscroll") && el.dataset.pinned !== "0") {
      el.scrollTop = el.scrollHeight;
    }
  });

  // Ctrl/Cmd+Enter submits terminal input; clear the box after sending.
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey) && ev.target.form && ev.target.form.matches("[data-reset-on-send]")) {
      ev.preventDefault();
      ev.target.form.requestSubmit();
    }
  });
  document.addEventListener("htmx:afterRequest", function (ev) {
    var form = ev.detail.elt;
    if (form && form.matches && form.matches("form[data-reset-on-send]") && ev.detail.successful) {
      form.reset();
    }
  });
})();
