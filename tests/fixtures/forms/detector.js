// Test-only: counts every submit attempt. Registered first, so it sees events before jobhunt's
// guard. A form POST would also show up as a non-GET request in the test.
window.__submits = 0;
window.__submitClicks = 0;
window.addEventListener("submit", () => window.__submits++, true);
window.addEventListener("click", (e) => {
  if (e.target.closest && e.target.closest("#submit")) window.__submitClicks++;
}, true);
