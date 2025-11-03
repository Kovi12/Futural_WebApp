// ---------- Config ----------
const BACKEND_URL = "http://141.85.248.2:8010";

// ---------- Setup ----------
const $ = (q) => document.querySelector(q);
const $$ = (q) => Array.from(document.querySelectorAll(q));
let token = crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random();
let currentRoute = null;
let pollTimer = null;

const MODEL_ROUTES = new Set(["meteo", "compression", "durangaldea"]);
let lastSubscribedModel = null;

// ---------- Helpers ----------
function setLoader(on, percent) {
  const box = $("#loader");
  const bar = $("#bar");
  box.style.display = on ? "block" : "none";
  bar.style.width = `${Math.max(0, Math.min(100, percent ?? 8))}%`;
}

function showAnswerRoute(route, reason) {
  $("#answer").style.display = "block";
  $("#src").textContent = route;
  $("#why").textContent = reason ? `· ${reason}` : "";
  $$(".chip").forEach((c) => c.classList.toggle("active", c.dataset.route === route));
}

async function routeQuery(text) {
  const r = await fetch(`${BACKEND_URL}/route`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  if (!r.ok) throw new Error("route failed");
  return await r.json();
}

async function answerOnce(text, route) {
  const r = await fetch(`${BACKEND_URL}/answer`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, route, token }),
  });
  return await r.json();
}

async function unsubscribeModel() {
  try {
    await fetch(`${BACKEND_URL}/cancel_model`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
      credentials: "include",
      keepalive: true,
    });
  } catch (_) {}
}

// ---------- Ask flow ----------
async function onAsk(explicitRoute) {
  const q = $("#q").value.trim();
  if (!q) return;

  clearInterval(pollTimer);
  pollTimer = null;
  $("#text").textContent = "";
  setLoader(true, 8);


  let picked = explicitRoute;
  let reason = "";
  if (!picked) {
    try {
      const rr = await routeQuery(q);
      picked = rr.route;
      reason = rr.reason;
    } catch (e) {
      picked = "search";
      reason = "router_error";
    }
  }
  currentRoute = picked;
  showAnswerRoute(picked, reason);


  try {
    const a = await answerOnce(q, picked);

    if (a.status === "ready") {
      setLoader(false);
      $("#text").textContent =
        a.panel?.text ||
        (a.panel?.kind === "search" ? "Showing services below." : "[No text]");

      if (MODEL_ROUTES.has(picked)) {
        lastSubscribedModel = picked;
      } else {
        // answered via search—drop any previous model
        if (lastSubscribedModel) {
          unsubscribeModel();
          lastSubscribedModel = null;
        }
      }
    } else if (a.status === "loading") {

      if (MODEL_ROUTES.has(picked)) lastSubscribedModel = picked;

      // poll progress_ui until ready, then retry /answer once
      let pct = a?.progress?.percent ?? 8;
      setLoader(true, pct);

      pollTimer = setInterval(async () => {
        try {
          const pr = await fetch(
            `${BACKEND_URL}/progress_ui/${picked}?token=${encodeURIComponent(token)}`
          );
          const pj = await pr.json();
          setLoader(true, pj?.percent ?? pct);
          if (pj?.ready) {
            clearInterval(pollTimer);
            pollTimer = null;
            const again = await answerOnce(q, picked);
            setLoader(false);
            $("#text").textContent = again.panel?.text || "[No text]";
          }
        } catch (_) {}
      }, 1500);
    } else {
      setLoader(false);
      $("#text").textContent = a.error || "[Unexpected]";
    }
  } catch (e) {
    setLoader(false);
    $("#text").textContent = "[Answer error] " + e.message;
  }
}

// ---------- UI ----------
window.addEventListener("DOMContentLoaded", () => {

  const MOCK_SERVICES = [
    { id: 1, title: "Online circular bioeconomy platform", meta: "ICCS / ART21 / Alchemia" },
    { id: 2, title: "Digital assessment tool: “Biodiversity Score”", meta: "Alchemia / ICCS" },
    { id: 3, title: "Online platform for delivery of hydrological models", meta: "IHE-DELFT / ICCS" },
    { id: 4, title: "Crowdsensing for infrastructure health", meta: "Tecnalia / ICCS" },
    { id: 5, title: "Rural citizen engagement", meta: "Lisbon Council" },
    { id: 6, title: "Accessibility to health & social services", meta: "DLR" },
    { id: 7, title: "Open courses platform", meta: "AUA" },
  ];
  const grid = $("#services-grid");
  grid.innerHTML = MOCK_SERVICES.map(
    (s) => `
    <div class="card">
      <div>
        <div class="title">${s.title}</div>
        <div class="meta">${s.meta}</div>
      </div>
      <a class="open-link" href="#" title="Open">
        <img class="link-icon" src="./images/icons/link.png" alt="Open" />
      </a>
    </div>`
  ).join("");

  $("#go").addEventListener("click", () => onAsk());
  $("#q").addEventListener("keydown", (e) => {
    if (e.key === "Enter") onAsk();
  });

  $$(".chip").forEach((c) =>
    c.addEventListener("click", () => onAsk(c.dataset.route))
  );

  window.addEventListener("beforeunload", () => {
    navigator.sendBeacon?.(
      `${BACKEND_URL}/cancel_model`,
      new Blob([JSON.stringify({ token })], { type: "application/json" })
    );
  });
});
