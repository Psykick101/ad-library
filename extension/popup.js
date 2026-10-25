const $ = id => document.getElementById(id);
chrome.storage.local.get("defaults").then(({defaults = {}}) => { $("tags").value = defaults.tags || ""; $("note").value = defaults.note || ""; });
const keep = () => chrome.storage.local.set({defaults: {tags: $("tags").value, note: $("note").value}});
$("tags").onchange = keep; $("note").onchange = keep;
$("area").onclick = async () => { keep(); chrome.runtime.sendMessage({do: "area"}); window.close(); };
$("shot").onclick = async () => { keep(); chrome.runtime.sendMessage({do: "shot", tags: $("tags").value, note: $("note").value}); window.close(); };
$("post").onclick = async () => { keep(); chrome.runtime.sendMessage({do: "post"}); window.close(); };
