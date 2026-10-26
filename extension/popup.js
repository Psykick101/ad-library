const $ = id => document.getElementById(id);
const send = m => new Promise(res => chrome.runtime.sendMessage(m, r => res(r || {})));
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const LABEL = {queued: "Queued", downloading: "Downloading", reading: "AI reading", done: "In Inspiration", failed: "Failed", gone: "Removed"};
const ago = t => { const m = (Date.now() - t) / 60000; return m < 1 ? "just now" : m < 60 ? Math.round(m) + " min ago" : m < 1440 ? Math.round(m / 60) + " h ago" : Math.round(m / 1440) + " d ago"; };
let ST = {};

chrome.storage.local.get("defaults").then(({defaults = {}}) => { $("tags").value = defaults.tags || ""; $("note").value = defaults.note || ""; });
const keep = () => chrome.storage.local.set({defaults: {tags: $("tags").value, note: $("note").value}});
$("tags").onchange = keep; $("note").onchange = keep;
$("area").onclick = async () => { keep(); await send({do: "area"}); window.close(); };
$("shot").onclick = async () => { keep(); await send({do: "shot", tags: $("tags").value, note: $("note").value}); window.close(); };
$("post").onclick = async () => { keep(); await send({do: "post"}); window.close(); };
$("connect").onclick = async () => { await send({do: "connect"}); window.close(); };

function renderRecent(recent) {
  const r = (recent || []).slice(0, 8);
  $("rech").hidden = !r.length;
  $("rec").innerHTML = r.map(x => `<div class="it"><div class="r"><span class="pill ${esc(x.status)}">${esc(LABEL[x.status] || x.status)}</span>
    <span>${esc(x.platform || "")}</span><span style="margin-left:auto;color:var(--mut)">${ago(x.ts)}</span></div>
    <div class="t" title="${esc(x.url)}">${esc(x.title || x.url || "")}</div>
    ${x.status === "failed" ? `<div class="err">${esc(x.error || "Couldn't download it")} <button class="l" data-retry="${esc(x.id)}">Try again</button></div>` : ""}</div>`).join("");
  document.querySelectorAll("[data-retry]").forEach(b => b.onclick = async () => { b.disabled = true; await send({do: "retry", id: b.dataset.retry}); refresh(); });
}

async function refresh() {
  ST = await send({do: "state"});
  const on = !!ST.email;
  $("who").textContent = on ? ST.email : "not connected";
  $("cn").hidden = on;
  for (const id of ["area", "shot"]) $(id).disabled = !on;
  $("post").disabled = !on;
  $("post").textContent = ST.post ? `Save this ${ST.post.platform} video` : "Save this page's video";
  $("post").title = ST.post ? ST.post.url : "Open a post (TikTok, Instagram, Facebook, YouTube Shorts, X…) or a feed with a playing video";
  $("lib").href = (ST.studio || "https://studio.psykick.cc") + "/studio#inspiration";
  renderRecent(ST.recent);
  foot(ST.latest);
}

// version + update (footer)
const nv = (a, b) => { const x = String(a).split(".").map(Number), y = String(b).split(".").map(Number);
  for (let i = 0; i < 3; i++) { if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) > (y[i] || 0); } return false; };
function foot(latest) {
  const f = $("upd"), V = chrome.runtime.getManifest().version;
  const dis = ST.email ? ` · <button class="l" id="dis">Disconnect</button>` : "";
  if (latest && latest.version && nv(latest.version, V)) {
    f.innerHTML = `<div>Version <b>${esc(V)}</b> · <b>${esc(latest.version)}</b> is out${latest.notes ? ": " + esc(latest.notes) : ""}</div>
      <button id="u1">1. Download the update</button><button class="s" id="u2">2. I've unzipped it over the old folder - reload</button>
      <span>Unzip into the same folder you installed from (replace the files), then press 2.</span>`;
    $("u1").onclick = () => chrome.tabs.create({url: (ST.studio || "https://studio.psykick.cc") + "/ui/psykick-saver.zip"});
    $("u2").onclick = () => send({do: "reload"});
  } else {
    f.innerHTML = `<div>Version <b>${esc(V)}</b> · up to date <button class="l" id="u0">check now</button>${dis}</div>`;
    $("u0").onclick = async () => { f.textContent = "Checking…"; const r = await send({do: "check"}); foot(r.latest); };
  }
  $("dis") && ($("dis").onclick = async () => { await send({do: "disconnect"}); refresh(); });
}

refresh();
send({do: "poll"}).then(r => r.recent && renderRecent(r.recent));
setInterval(async () => { const r = await send({do: "poll"}); if (r.recent) renderRecent(r.recent); }, 3000);
