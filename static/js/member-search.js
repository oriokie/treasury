/* Member/membership search — upgrades a plain <select> rendered
   from a ModelChoiceField into a type-and-search box, WITHOUT changing what
   the form actually submits: the <select> stays in the DOM (just hidden)
   and still carries the field's real `name`, so no server-side change is
   needed to accept it. Picking a suggestion sets the <select>'s value; the
   text box is purely a friendlier way to reach the same option a giant
   dropdown would have offered.

   One shared implementation, reused across every form that needs it —
   benevolent (register, contribution, case) and pledges so far. It was
   called `benevolent-search.js` while it was only used there; the name is
   now the honest one, because nothing about it is benevolent-specific and
   a misleading name is how a second, duplicate copy gets written by someone
   who did not think to look inside a module they were not working on.

   Usage:
     MemberSearch.enhance({
       selectId: "id_member",          // the <select>'s id (Django default: id_<field>)
       searchUrl: "/benevolent/search/members/",
       placeholder: "Start typing a member's name…",
       params: {},                     // extra fixed query params, e.g. {scheme: 3}
       renderLabel: function(r){ return r.name + (r.phone ? " · " + r.phone : ""); },
       renderMeta: function(r){ return r.type || r.number || ""; },
     });
*/
(function (window) {
  "use strict";

  function enhance(opts) {
    var select = document.getElementById(opts.selectId);
    if (!select) return;

    var wrap = document.createElement("div");
    wrap.className = "pos-rel";
    select.parentNode.insertBefore(wrap, select);
    wrap.appendChild(select);
    select.style.display = "none";
    select.setAttribute("aria-hidden", "true");

    var input = document.createElement("input");
    input.type = "text";
    input.className = "field";
    input.autocomplete = "off";
    input.placeholder = opts.placeholder || "Start typing…";
    input.id = opts.selectId + "_search";
    // A hidden <select> never shows the browser's native "please fill out
    // this field" validation UI — most browsers silently skip validating
    // an invisible control, so a required field left empty could submit
    // (and bounce back from the server) with no visible warning at the
    // point of clicking the button at all. Moving `required` onto the
    // visible search box the person is actually looking at restores that
    // native, impossible-to-miss cue; the select's own requiredness still
    // backs it up server-side regardless of what the browser enforces.
    if (select.required) {
      input.required = true;
      select.required = false;
    }

    var box = document.createElement("div");
    box.className = "ac-box";
    box.id = opts.selectId + "_ac";

    wrap.insertBefore(input, select);
    wrap.appendChild(box);

    // If the select already has a value (editing, or a server-side default),
    // show its current label so the box isn't blank for no reason.
    var current = select.options[select.selectedIndex];
    if (current && current.value) {
      input.value = current.textContent.trim();
    }

    var timer = null;
    var lastPicked = input.value;

    function query(q) {
      var url = opts.searchUrl + "?q=" + encodeURIComponent(q);
      Object.keys(opts.params || {}).forEach(function (k) {
        url += "&" + encodeURIComponent(k) + "=" + encodeURIComponent(opts.params[k]);
      });
      // Unwrap the envelope HERE, so every caller downstream gets a plain array.
      // This was a real bug: query() resolved to the whole {results: [...]}
      // object and it was passed straight into renderResults(), which tests
      // `results.length` — undefined on an object, so the box was hidden and
      // the function returned, every single time. The widget never displayed a
      // single suggestion to anybody; the endpoint was fine and the CSS was
      // fine, and the request was even being made — the answer was just being
      // thrown away one line before it could be rendered.
      return fetch(url)
        .then(function (r) { return r.ok ? r.json() : { results: [] }; })
        .then(function (d) { return (d && d.results) || []; });
    }

    function renderResults(results) {
      box.innerHTML = "";
      if (!results.length) { box.style.display = "none"; return; }
      results.forEach(function (r) {
        var item = document.createElement("div");
        item.className = "ac-item" + (r.warning ? " ac-warn" : "");
        var label = opts.renderLabel ? opts.renderLabel(r) : r.name;
        var meta = opts.renderMeta ? opts.renderMeta(r) : "";
        var html = escapeHtml(label);
        if (meta) html += ' <span class="muted">· ' + escapeHtml(meta) + "</span>";
        // A candidate who is already enrolled here, or is already on record as
        // somebody else's spouse or dependant, is very often not a new
        // principal member at all — the registrar should see that BEFORE they
        // commit, not discover it afterwards.
        if (r.warning) {
          html += '<div class="ac-note">⚠ ' + escapeHtml(r.warning) + "</div>";
        }
        item.innerHTML = html;
        var pick = function () {
          // The <select> may be empty by design. A typeahead over a roll of
          // thousands has no business rendering every member as an <option>,
          // and assigning .value for an option that does not exist silently
          // leaves the select blank — the form then rejects a member the user
          // can plainly see they picked. So the chosen one is added first.
          if (!select.querySelector('option[value="' + r.id + '"]')) {
            var opt = document.createElement("option");
            opt.value = r.id;
            opt.textContent = label;
            select.appendChild(opt);
          }
          select.value = r.id;
          input.value = label;
          lastPicked = label;
          box.style.display = "none";
          if (opts.onPick) opts.onPick(r);
          select.dispatchEvent(new Event("change", { bubbles: true }));
        };
        item.addEventListener("mousedown", function (e) { e.preventDefault(); pick(); });
        item._pick = pick;
        box.appendChild(item);
      });
      box.style.display = "block";
    }

    function escapeHtml(s) {
      var d = document.createElement("div");
      d.textContent = s == null ? "" : String(s);
      return d.innerHTML;
    }

    input.addEventListener("input", function () {
      clearTimeout(timer);
      var q = input.value.trim();
      if (q !== lastPicked) select.value = "";   // typing again invalidates the old pick
      if (q.length < 2) { box.style.display = "none"; return; }
      timer = setTimeout(function () {
        query(q).then(renderResults).catch(function () { box.style.display = "none"; });
      }, 180);
    });

    input.addEventListener("blur", function () {
      setTimeout(function () { box.style.display = "none"; }, 150);
    });

    input.addEventListener("keydown", function (e) {
      var open = box.style.display === "block";
      var items = [].slice.call(box.querySelectorAll(".ac-item"));
      if (e.key === "Enter" && open && items.length) {
        var act = box.querySelector(".ac-item.ac-active") || items[0];
        e.preventDefault();
        if (act._pick) act._pick();
        return;
      }
      if (!open || !items.length) return;
      var idx = items.findIndex(function (i) { return i.classList.contains("ac-active"); });
      if (e.key === "ArrowDown") { e.preventDefault(); idx = (idx + 1) % items.length; }
      else if (e.key === "ArrowUp") { e.preventDefault(); idx = (idx - 1 + items.length) % items.length; }
      else if (e.key === "Escape") { box.style.display = "none"; return; }
      else return;
      items.forEach(function (i) { i.classList.remove("ac-active"); });
      items[idx].classList.add("ac-active");
      items[idx].scrollIntoView({ block: "nearest" });
    });
  }

  window.MemberSearch = { enhance: enhance };
  // Kept so nothing that already calls the old name breaks.
  window.BenevolentSearch = window.MemberSearch;
})(window);
