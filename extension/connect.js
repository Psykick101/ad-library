// Runs only on the Studio's "Connect the extension" page: hands the new token from that page to the extension
// (fallback for when the page can't message the extension directly). Same window + same origin only.
window.addEventListener("message", e => {
  if (e.source !== window || e.origin !== location.origin || !e.data || e.data.psykick !== "connect") return;
  const {token, email, id, studio} = e.data;
  chrome.runtime.sendMessage({do: "connect-token", token, email, id, studio}, r => {
    if (!chrome.runtime.lastError && r && r.ok) window.postMessage({psykick: "connected"}, location.origin);
  });
});
