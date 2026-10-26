// node tests/ext/canon.test.js - finder.js canonical() against the same table as library_service.canonical()
const F = require("../../extension/finder.js");
const cases = require("./canon_cases.json");
let ok = 0, n = 0;
for (const [u, want] of cases) {
  n++; const got = (F.canonical(u) || {}).key || null;
  if (got === want) ok++; else console.log("FAIL", u, "->", got, "want", want);
}
const c = F.canonical("https://www.instagram.com/reels/Cabc123xyz/?igsh=1");
n++; if (c.url === "https://www.instagram.com/reel/Cabc123xyz/") ok++; else console.log("FAIL clean url", c.url);
console.log(`${ok}/${n} OK`); process.exit(ok === n ? 0 : 1);
