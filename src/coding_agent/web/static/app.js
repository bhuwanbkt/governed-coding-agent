// Live updates without third-party code (the page's CSP allows only this site's scripts).
// An element with data-poll="<url>" is refreshed every data-every ms with server-rendered,
// escaped HTML from our own origin. The server sends "X-Poll-Stop: 1" when the job is done;
// with data-reload-on-stop the page reloads once to show the result (diff, decision).
document.querySelectorAll("[data-poll]").forEach((el) => {
  const url = el.dataset.poll;
  const every = Number(el.dataset.every || 3000);
  const tick = async () => {
    try {
      const response = await fetch(url, { credentials: "same-origin" });
      if (response.redirected || response.status === 401) return; // session ended
      if (response.ok) el.innerHTML = await response.text();
      if (response.headers.get("X-Poll-Stop") === "1") {
        if (el.dataset.reloadOnStop) window.location.reload();
        return;
      }
    } catch (_) {
      // network blip: try again on the next tick
    }
    window.setTimeout(tick, every);
  };
  window.setTimeout(tick, every);
});
