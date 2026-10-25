const $ = id => document.getElementById(id);
chrome.storage.local.get("defaults").then(({defaults = {}}) => { $("tags").value = defaults.tags || ""; $("note").value = defaults.note || ""; });
const keep = () => chrome.storage.local.set({defaults: {tags: $("tags").value, note: $("note").value}});
$("tags").onchange = keep; $("note").onchange = keep;
$("area").onclick = async () => { keep(); chrome.runtime.sendMessage({do: "area"}); window.close(); };
$("shot").onclick = async () => { keep(); chrome.runtime.sendMessage({do: "shot", tags: $("tags").value, note: $("note").value}); window.close(); };
$("post").onclick = async () => { keep(); chrome.runtime.sendMessage({do: "post"}); window.close(); };

// version + update (footer)
const V = chrome.runtime.getManifest().version;
const nv = (a, b) => { const x = a.split(".").map(Number), y = b.split(".").map(Number);
  for (let i = 0; i < 3; i++) { if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) > (y[i] || 0); } return false; };
function foot(latest) {
  const f = $("upd");
  if (latest && latest.version && nv(latest.version, V)) {
    f.innerHTML = `<div>Version <b>${V}</b> · <b>${latest.version}</b> is out${latest.notes ? ": " + latest.notes : ""}</div>
      <button id="u1">1. Download the update</button><button class="s" id="u2">2. I've unzipped it over the old folder - reload</button>
      <span>Unzip into the same folder you installed from (replace the files), then press 2.</span>`;
    $("u1").onclick = () => chrome.tabs.create({url: "https://studio.psykick.cc/ui/psykick-saver.zip"});
    $("u2").onclick = () => chrome.runtime.sendMessage({do: "reload"});
  } else {
    f.innerHTML = `<div>Version <b>${V}</b> · up to date <a href="#" id="u0">check now</a></div>`;
    $("u0").onclick = e => { e.preventDefault(); f.textContent = "Checking…"; chrome.runtime.sendMessage({do: "check"}, r => foot(r && r.latest)); };
  }
}
chrome.storage.local.get("latest").then(({latest}) => foot(latest));
