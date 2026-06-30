/* Appearance & Preferences — live apply + auto-save.
   Applies changes to <html> instantly and persists each change to the server
   via /preferences/update/. Modular: every control is wired by data-pref. */
(function () {
  "use strict";
  var root = document.documentElement;
  var form = document.getElementById("prefForm");
  if (!form) return;

  function csrf() {
    var el = form.querySelector("[name=csrfmiddlewaretoken]");
    return el ? el.value : "";
  }

  function save(key, value) {
    var body = new URLSearchParams();
    body.append("csrfmiddlewaretoken", csrf());
    body.append("key", key);
    body.append("value", value);
    return fetch(form.dataset.url || "/preferences/update/", {
      method: "POST", headers: { "X-Requested-With": "XMLHttpRequest" },
      body: body
    }).then(function (r) { return r.ok ? r.json() : Promise.reject(r); })
      .catch(function () {
        if (window.toast) window.toast("Couldn't save that change.", "error");
      });
  }

  // Map a preference key+value onto the <html> data-attributes (live preview).
  function apply(key, value) {
    var v = String(value).toLowerCase();
    switch (key) {
      case "theme": root.setAttribute("data-theme", v); break;
      case "sidebar": root.setAttribute("data-sidebar", v); break;
      case "font_size": root.setAttribute("data-font", v); break;
      case "font_family": root.setAttribute("data-fontfamily", String(v).toLowerCase()); break;
      case "layout_width": root.setAttribute("data-width", v); break;
      case "card_style": root.setAttribute("data-cards", v); break;
      case "density": root.setAttribute("data-density", v); break;
      case "accent":
        root.setAttribute("data-accent", v); break;
      case "high_contrast":
        value ? root.setAttribute("data-contrast", "high") : root.removeAttribute("data-contrast"); break;
      case "reduced_motion":
        value ? root.setAttribute("data-motion", "reduced") : root.removeAttribute("data-motion"); break;
      case "large_targets":
        value ? root.setAttribute("data-targets", "large") : root.removeAttribute("data-targets"); break;
      case "focus_indicators":
        value ? root.removeAttribute("data-focus") : root.setAttribute("data-focus", "off"); break;
    }
  }

  // --- Tabs ---
  document.querySelectorAll(".pref-tabs .pt").forEach(function (tab) {
    tab.addEventListener("click", function () {
      var pane = tab.dataset.pane;
      document.querySelectorAll(".pref-tabs .pt").forEach(function (t) {
        var on = t === tab; t.classList.toggle("active", on);
        t.setAttribute("aria-selected", on ? "true" : "false");
      });
      document.querySelectorAll(".pref-pane").forEach(function (p) {
        p.hidden = p.dataset.pane !== pane;
      });
    });
  });

  // --- Segmented controls ---
  document.querySelectorAll(".seg[data-pref]").forEach(function (seg) {
    var key = seg.dataset.pref;
    seg.querySelectorAll("button[data-val]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        seg.querySelectorAll("button").forEach(function (b) { b.classList.remove("on"); });
        btn.classList.add("on");
        apply(key, btn.dataset.val);
        save(key, btn.dataset.val);
      });
    });
  });

  // --- Accent swatches ---
  var picker = document.querySelector(".accent-picker[data-pref=accent]");
  if (picker) {
    picker.querySelectorAll(".swatch[data-val]").forEach(function (sw) {
      sw.addEventListener("click", function () {
        picker.querySelectorAll(".swatch").forEach(function (s) { s.classList.remove("on"); });
        sw.classList.add("on");
        root.style.setProperty("--pref-accent", sw.dataset.hex);
        apply("accent", sw.dataset.val);
        save("accent", sw.dataset.val);
      });
    });
    var custom = document.getElementById("accentCustom");
    if (custom) {
      custom.addEventListener("input", function () {
        picker.querySelectorAll(".swatch").forEach(function (s) { s.classList.remove("on"); });
        custom.closest(".swatch").classList.add("on");
        root.style.setProperty("--pref-accent", custom.value);
        root.setAttribute("data-accent", "custom");
        save("accent_custom", custom.value);
        save("accent", "custom");
      });
    }
  }

  // --- Boolean switches ---
  document.querySelectorAll(".pref-bool[data-pref]").forEach(function (cb) {
    cb.addEventListener("change", function () {
      apply(cb.dataset.pref, cb.checked);
      save(cb.dataset.pref, cb.checked ? "1" : "0");
    });
  });

  // --- Selects ---
  document.querySelectorAll("select[data-pref]").forEach(function (sel) {
    sel.addEventListener("change", function () {
      apply(sel.dataset.pref, sel.value);
      save(sel.dataset.pref, sel.value);
    });
  });

  // --- Number inputs ---
  document.querySelectorAll("input[type=number][data-pref]").forEach(function (inp) {
    inp.addEventListener("change", function () { save(inp.dataset.pref, inp.value); });
  });

  // --- Dashboard widgets: visibility + drag reorder ---
  var list = document.getElementById("widgetList");
  if (list) {
    function persistWidgets() {
      var data = [].map.call(list.querySelectorAll(".wl-item"), function (li) {
        return { key: li.dataset.key, visible: li.querySelector(".wl-vis").checked };
      });
      var body = new URLSearchParams();
      body.append("csrfmiddlewaretoken", csrf());
      body.append("key", "dashboard_widgets");
      body.append("value", JSON.stringify(data));
      fetch("/preferences/update/", { method: "POST", body: body }).catch(function () {});
    }
    list.querySelectorAll(".wl-vis").forEach(function (cb) {
      cb.addEventListener("change", persistWidgets);
    });
    var dragEl = null;
    list.addEventListener("dragstart", function (e) {
      var li = e.target.closest(".wl-item"); if (!li) return;
      dragEl = li; li.classList.add("dragging");
    });
    list.addEventListener("dragend", function () {
      if (dragEl) dragEl.classList.remove("dragging");
      list.querySelectorAll(".drop-target").forEach(function (x) { x.classList.remove("drop-target"); });
      dragEl = null; persistWidgets();
    });
    list.addEventListener("dragover", function (e) {
      e.preventDefault();
      var li = e.target.closest(".wl-item");
      if (!li || li === dragEl) return;
      var rect = li.getBoundingClientRect();
      var after = (e.clientY - rect.top) > rect.height / 2;
      list.insertBefore(dragEl, after ? li.nextSibling : li);
    });
  }

  // --- Sample toast ---
  var test = document.getElementById("testToast");
  if (test) test.addEventListener("click", function () {
    if (window.toast) window.toast("This is a sample notification.", "success");
    else alert("Toasts are disabled.");
    if (window.__prefs && window.__prefs.desktop && "Notification" in window
        && Notification.permission === "default") {
      Notification.requestPermission();
    }
  });

  form.dataset.url = "/preferences/update/";
})();
