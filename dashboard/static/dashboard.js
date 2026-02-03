const themeToggle = document.getElementById("theme-toggle");
const rows = Array.from(document.querySelectorAll(".job-row"));
const searchInput = document.getElementById("filter-search");
const sourceSelect = document.getElementById("filter-source");
const unnotifiedToggle = document.getElementById("filter-unnotified");
const recentToggle = document.getElementById("filter-recent");
const resetButton = document.getElementById("filter-reset");
const filterCount = document.getElementById("filter-count");

const MS_IN_HOUR = 1000 * 60 * 60;
const RECENT_WINDOW_HOURS = 24;

function resolveTheme() {
  const stored = localStorage.getItem("dashboard-theme");
  if (stored) {
    return stored;
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function applyTheme(theme) {
  document.body.dataset.theme = theme;
  localStorage.setItem("dashboard-theme", theme);
}

if (themeToggle) {
  applyTheme(resolveTheme());
  themeToggle.addEventListener("click", () => {
    const current = document.body.dataset.theme || "light";
    applyTheme(current === "dark" ? "light" : "dark");
  });
}

function parseTimestamp(value) {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : parsed;
}

function isRecent(row) {
  const posted = parseTimestamp(row.dataset.posted);
  const firstSeen = parseTimestamp(row.dataset.firstSeen);
  const basis = posted || firstSeen;
  if (!basis) return false;
  const now = Date.now();
  return now - basis <= RECENT_WINDOW_HOURS * MS_IN_HOUR;
}

function highlightRecent() {
  rows.forEach((row) => {
    if (isRecent(row)) {
      row.classList.add("recent");
    }
  });
}

function updateFilterCount(visible, total) {
  if (!filterCount) return;
  filterCount.textContent = `Showing ${visible} of ${total}`;
}

function applyFilters() {
  const query = (searchInput?.value || "").trim().toLowerCase();
  const source = (sourceSelect?.value || "").trim();
  const unnotifiedOnly = !!unnotifiedToggle?.checked;
  const recentOnly = !!recentToggle?.checked;

  let visible = 0;
  rows.forEach((row) => {
    const matchesSource = !source || row.dataset.source === source;
    const matchesQuery =
      !query ||
      row.dataset.title.includes(query) ||
      row.dataset.location.includes(query);
    const matchesUnnotified = !unnotifiedOnly || row.dataset.notified === "0";
    const matchesRecent = !recentOnly || isRecent(row);

    const show = matchesSource && matchesQuery && matchesUnnotified && matchesRecent;
    row.style.display = show ? "grid" : "none";
    if (show) {
      visible += 1;
    }
  });

  updateFilterCount(visible, rows.length);
}

function resetFilters() {
  if (searchInput) searchInput.value = "";
  if (sourceSelect) sourceSelect.value = "";
  if (unnotifiedToggle) unnotifiedToggle.checked = false;
  if (recentToggle) recentToggle.checked = false;
  applyFilters();
}

if (searchInput) {
  searchInput.addEventListener("input", applyFilters);
}
if (sourceSelect) {
  sourceSelect.addEventListener("change", applyFilters);
}
if (unnotifiedToggle) {
  unnotifiedToggle.addEventListener("change", applyFilters);
}
if (recentToggle) {
  recentToggle.addEventListener("change", applyFilters);
}
if (resetButton) {
  resetButton.addEventListener("click", resetFilters);
}

highlightRecent();
applyFilters();
