const { JSDOM } = require("jsdom");
const fs = require("fs");
const path = require("path");

// Resolved from this file, not an absolute path. It used to point at
// /home/claude/treasury/... — a path from the machine it was written on — so
// the test could not run on any other checkout, including CI.
const src = fs.readFileSync(
  path.resolve(__dirname, "..", "..", "static", "js", "member-search.js"), "utf8");

const dom = new JSDOM(`<!DOCTYPE html><body>
  <form><div class="form-row">
    <select id="id_member" name="member" required>
      <option value="">---------</option>
      <option value="7">MARY WANJIRU</option>
    </select>
  </div>
  <input id="id_member_name"><input id="id_member_phone">
  </form></body>`, { runScripts: "outside-only" });

const { window } = dom;
window.eval(src);

// stub fetch: returns the SAME envelope shape the real endpoint returns
let lastUrl = null;
window.fetch = (url) => {
  lastUrl = url;
  return Promise.resolve({
    ok: true,
    json: () => Promise.resolve({ results: [
      { id: 7, name: "MARY WANJIRU", phone: "254711000111", type: "Church member", warning: "" },
      { id: 9, name: "MARY OTIENO", phone: "254722000222", type: "Visitor",
        warning: "Already registered as spouse of JOHN OTIENO (BEN-2026-0004)." },
    ]})
  });
};

let picked = null;
window.MemberSearch.enhance({
  selectId: "id_member",
  searchUrl: "/benevolent/search/members/",
  params: { scheme: 1 },
  placeholder: "type…",
  renderLabel: (r) => r.name,
  renderMeta: (r) => [r.type, r.phone].filter(Boolean).join(" · "),
  onPick: (r) => { picked = r; },
});

const input = window.document.getElementById("id_member_search");
const box = window.document.getElementById("id_member_ac");
const select = window.document.getElementById("id_member");

let pass = 0, fail = 0;
const ok = (c, m) => { if (c) { pass++; console.log("  ok  " + m); }
                       else { fail++; console.log("  FAIL " + m); } };

ok(!!input, "a search input was created");
ok(select.style.display === "none", "the original select is hidden");
ok(input.required === true && select.required === false,
   "required moved to the visible input (hidden selects skip native validation)");

// type "mar" -> should fetch and RENDER
input.value = "mar";
input.dispatchEvent(new window.Event("input", { bubbles: true }));

setTimeout(() => {
  ok(lastUrl && lastUrl.includes("q=mar"), "the search request was made");
  ok(lastUrl && lastUrl.includes("scheme=1"), "extra params were sent");

  setTimeout(() => {
    const items = box.querySelectorAll(".ac-item");
    ok(box.style.display === "block",
       "THE BUG: the results box is actually SHOWN (was always hidden)");
    ok(items.length === 2, `both suggestions rendered (got ${items.length})`);
    ok(items[0].textContent.includes("MARY WANJIRU"), "first suggestion has the name");
    ok(items[0].textContent.includes("254711000111"), "…and the phone");
    ok(items[1].classList.contains("ac-warn"),
       "the already-a-spouse candidate is flagged");
    ok(items[1].textContent.includes("spouse of JOHN OTIENO"),
       "…and says whose household they are already in");

    // pick the first
    items[0].dispatchEvent(new window.Event("mousedown", { bubbles: true, cancelable: true }));
    ok(select.value === "7", "picking sets the underlying select's value");
    ok(input.value === "MARY WANJIRU", "…and fills the visible box");
    ok(box.style.display === "none", "…and closes the popup");
    ok(picked && picked.id === 7, "onPick fired with the chosen record");

    // XSS safety
    window.fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve({
      results: [{ id: 1, name: "<img src=x onerror=alert(1)>", phone: "", type: "" }] }) });
    input.value = "xss";
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
    setTimeout(() => {
      const it = box.querySelector(".ac-item");
      ok(it && it.querySelector("img") === null,
         "a name containing HTML is escaped, not injected");
      console.log(`\n${pass} passed, ${fail} failed`);
      process.exit(fail ? 1 : 0);
    }, 400);
  }, 300);
}, 250);
