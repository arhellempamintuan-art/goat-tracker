// Herd Watch dashboard client
// Polls /api/goats for live status and /api/history/<id> + /api/goats/<id>/track
// for the detail drawer.

const POLL_MS = 15000;
const DEFAULT_CENTER = [37.0, -95.7]; // fallback if no goats have a fix yet

let map;
let markers = {};       // goat_id -> L.marker
let currentGoatId = null;
let hrChart = null;
let tempChart = null;

// ---------- init ----------
document.addEventListener("DOMContentLoaded", () => {
  initMap();
  refreshGoats();
  setInterval(refreshGoats, POLL_MS);

  document.getElementById("drawer-close").addEventListener("click", closeDrawer);
});

function initMap() {
  map = L.map("map", { zoomControl: true }).setView(DEFAULT_CENTER, 4);
  L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
    attribution: '&copy; OpenStreetMap contributors &copy; CARTO',
    maxZoom: 19,
  }).addTo(map);
}

// ---------- data refresh ----------
async function refreshGoats() {
  try {
    const res = await fetch("/api/goats");
    if (!res.ok) throw new Error("bad response");
    const data = await res.json();
    renderHerd(data.goats || []);
    setSyncState("live");
  } catch (err) {
    console.error(err);
    setSyncState("down");
  }
}

function setSyncState(state) {
  const dot = document.getElementById("sync-dot");
  const label = document.getElementById("sync-label");
  dot.className = "dot " + (state === "live" ? "live" : state === "stale" ? "stale" : "down");
  const now = new Date();
  const timeStr = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  label.textContent = state === "live" ? `synced ${timeStr}` : "connection lost";
}

// ---------- render herd ----------
function renderHerd(goats) {
  const counts = { ok: 0, warning: 0, alert: 0, unknown: 0 };
  let boundsPoints = [];

  const cardsEl = document.getElementById("goat-cards");
  cardsEl.innerHTML = "";

  goats.forEach((goat) => {
    const status = goat.stale ? "unknown" : goat.status;
    counts[status] = (counts[status] || 0) + 1;

    // --- map marker ---
    if (goat.gps && goat.gps.valid && goat.gps.lat && goat.gps.lon) {
      const latlng = [goat.gps.lat, goat.gps.lon];
      boundsPoints.push(latlng);
      upsertMarker(goat.goat_id, latlng, status, goat);
    }

    // --- sidebar card ---
    cardsEl.appendChild(buildGoatCard(goat, status));
  });

  document.getElementById("stat-total").textContent = goats.length;
  document.getElementById("stat-ok").textContent = counts.ok || 0;
  document.getElementById("stat-warning").textContent = counts.warning || 0;
  document.getElementById("stat-alert").textContent = counts.alert || 0;

  if (boundsPoints.length > 0 && !map._userHasPanned) {
    map.fitBounds(boundsPoints, { padding: [40, 40], maxZoom: 15 });
  }

  // if the drawer is open for a goat, refresh its live metrics
  if (currentGoatId) {
    const g = goats.find((x) => x.goat_id === currentGoatId);
    if (g) updateDrawerMetrics(g);
  }
}

map_userPanTracking();
function map_userPanTracking() {
  // Only auto-fit bounds until the user manually interacts with the map
  document.addEventListener("DOMContentLoaded", () => {
    setTimeout(() => {
      if (!map) return;
      map.on("dragstart zoomstart", () => { map._userHasPanned = true; });
    }, 500);
  });
}

function upsertMarker(goatId, latlng, status, goat) {
  const icon = L.divIcon({
    className: "",
    html: `<div class="goat-marker goat-marker--${status}">🐐</div>`,
    iconSize: [30, 30],
  });

  if (markers[goatId]) {
    markers[goatId].setLatLng(latlng);
    markers[goatId].setIcon(icon);
  } else {
    markers[goatId] = L.marker(latlng, { icon }).addTo(map);
  }

  markers[goatId].bindPopup(popupHtml(goat, status));
  markers[goatId].off("click");
  markers[goatId].on("click", () => openDrawer(goat.goat_id));
}

function popupHtml(goat, status) {
  const temp = goat.temperature_valid ? `${goat.temperature_c.toFixed(1)}°C` : "—";
  const hr = goat.heart_rate_valid ? `${goat.heart_rate_bpm} bpm` : "—";
  return `<strong>${escapeHtml(goat.goat_id)}</strong><br/>${temp} · ${hr}`;
}

function buildGoatCard(goat, status) {
  const card = document.createElement("div");
  card.className = "goat-card";
  card.addEventListener("click", () => openDrawer(goat.goat_id));

  const temp = goat.temperature_valid ? `${goat.temperature_c.toFixed(1)}°C` : "–";
  const hr = goat.heart_rate_valid ? `${goat.heart_rate_bpm} bpm` : "–";
  const batt = goat.battery_v ? `${goat.battery_v.toFixed(2)}V` : "–";
  const timeAgo = timeSince(goat.received_at);

  card.innerHTML = `
    <div class="goat-card-top">
      <span class="goat-name">${escapeHtml(goat.goat_id)}</span>
      <span class="pill pill--${status}">${status}</span>
    </div>
    <div class="goat-card-metrics">
      <span>temp <b>${temp}</b></span>
      <span>hr <b>${hr}</b></span>
      <span>batt <b>${batt}</b></span>
    </div>
    <div class="goat-card-time">last update ${timeAgo}</div>
  `;
  return card;
}

// ---------- detail drawer ----------
async function openDrawer(goatId) {
  currentGoatId = goatId;
  document.getElementById("drawer-title").textContent = goatId;
  document.getElementById("detail-drawer").classList.add("open");
  document.getElementById("detail-drawer").setAttribute("aria-hidden", "false");

  try {
    const [historyRes, goatsRes] = await Promise.all([
      fetch(`/api/history/${encodeURIComponent(goatId)}?hours=24`),
      fetch(`/api/goats`),
    ]);
    const history = await historyRes.json();
    const goats = (await goatsRes.json()).goats || [];
    const goat = goats.find((g) => g.goat_id === goatId);

    if (goat) updateDrawerMetrics(goat);
    renderCharts(history.readings || []);
  } catch (err) {
    console.error(err);
  }
}

function updateDrawerMetrics(goat) {
  const status = goat.stale ? "unknown" : goat.status;
  const statusEl = document.getElementById("drawer-status");
  statusEl.textContent = status;
  statusEl.className = "pill pill--" + status;

  document.getElementById("drawer-temp").textContent = goat.temperature_valid
    ? `${goat.temperature_c.toFixed(1)}°C`
    : "no reading";
  document.getElementById("drawer-hr").textContent = goat.heart_rate_valid
    ? `${goat.heart_rate_bpm} bpm`
    : "no reading";
  document.getElementById("drawer-batt").textContent = goat.battery_v
    ? `${goat.battery_v.toFixed(2)} V`
    : "–";
  document.getElementById("drawer-coords").textContent =
    goat.gps && goat.gps.valid
      ? `${goat.gps.lat.toFixed(5)}, ${goat.gps.lon.toFixed(5)}`
      : "no fix";
}

function closeDrawer() {
  currentGoatId = null;
  document.getElementById("detail-drawer").classList.remove("open");
  document.getElementById("detail-drawer").setAttribute("aria-hidden", "true");
}

// ---------- charts ----------
function renderCharts(readings) {
  const labels = readings.map((r) =>
    new Date(r.received_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
  );
  const hrData = readings.map((r) => (r.heart_rate_valid ? r.heart_rate_bpm : null));
  const tempData = readings.map((r) => (r.temperature_valid ? r.temperature_c : null));

  const gridColor = "rgba(155,168,147,0.15)";
  const tickColor = "#9BA893";

  const commonOptions = {
    responsive: true,
    animation: false,
    plugins: { legend: { display: false } },
    scales: {
      x: { ticks: { color: tickColor, maxTicksLimit: 6 }, grid: { color: gridColor } },
      y: { ticks: { color: tickColor }, grid: { color: gridColor } },
    },
    spanGaps: true,
  };

  if (hrChart) hrChart.destroy();
  if (tempChart) tempChart.destroy();

  hrChart = new Chart(document.getElementById("chart-hr"), {
    type: "line",
    data: {
      labels,
      datasets: [{
        data: hrData,
        borderColor: "#C1443C",
        backgroundColor: "rgba(193,68,60,0.12)",
        fill: true,
        tension: 0.35,
        pointRadius: 0,
        borderWidth: 2,
      }],
    },
    options: commonOptions,
  });

  tempChart = new Chart(document.getElementById("chart-temp"), {
    type: "line",
    data: {
      labels,
      datasets: [{
        data: tempData,
        borderColor: "#E0A937",
        backgroundColor: "rgba(224,169,55,0.12)",
        fill: true,
        tension: 0.35,
        pointRadius: 0,
        borderWidth: 2,
      }],
    },
    options: commonOptions,
  });
}

// ---------- utils ----------
function timeSince(isoString) {
  if (!isoString) return "never";
  const seconds = Math.floor((Date.now() - new Date(isoString).getTime()) / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}
