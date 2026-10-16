(() => {
  "use strict";

  // Swap 422 validation responses (partials carrying an error banner); treat other 4xx/5xx as errors.
  htmx.config.responseHandling = [
    { code: "204", swap: false },
    { code: "[23]..", swap: true },
    { code: "422", swap: true },
    { code: "[45]..", swap: false, error: true },
    { code: "...", swap: true },
  ];

  function remaining(ms) {
    if (ms <= 0) return "now";
    const s = Math.ceil(ms / 1000);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    if (h) return `${h}h ${m}m`;
    if (m) return `${m}m ${sec}s`;
    return `${sec}s`;
  }

  function tickCountdowns() {
    document.querySelectorAll("[data-countdown]").forEach((el) => {
      const target = Date.parse(el.dataset.countdown);
      if (!Number.isNaN(target)) el.textContent = remaining(target - Date.now());
    });
  }

  function initSortables() {
    document.querySelectorAll("[data-sortable]").forEach((list) => {
      if (list._sortable) return;
      list._sortable = Sortable.create(list, {
        handle: ".drag",
        draggable: "[data-video-id]",
        animation: 150,
        onEnd() {
          const order = [...list.querySelectorAll("[data-video-id]")].map((el) => el.dataset.videoId).join(",");
          htmx.ajax("POST", list.dataset.sortable, { target: "#video-list", swap: "outerHTML", values: { order } });
        },
      });
    });
  }

  function followLog() {
    const log = document.getElementById("log");
    if (!log) return;
    while (log.children.length > 1000) log.firstElementChild.remove();
    const follow = document.getElementById("log-follow");
    if (!follow || follow.checked) log.scrollTop = log.scrollHeight;
  }

  function toggleTypeFields() {
    const type = document.querySelector("[name=type]");
    if (!type) return;
    document.querySelectorAll("[data-show-only]").forEach((el) => { el.hidden = type.value !== "show"; });
  }

  function showToast(text) {
    const toast = document.getElementById("toast");
    if (!toast) return;
    toast.textContent = text.slice(0, 300);
    toast.hidden = false;
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => { toast.hidden = true; }, 6000);
  }

  function refresh() { tickCountdowns(); initSortables(); followLog(); }

  document.addEventListener("DOMContentLoaded", () => { refresh(); toggleTypeFields(); });
  document.addEventListener("htmx:afterSettle", refresh);
  document.addEventListener("change", (e) => { if (e.target.matches("[name=type]")) toggleTypeFields(); });
  document.addEventListener("htmx:responseError", (e) => {
    const xhr = e.detail.xhr;
    showToast(xhr.responseText && xhr.responseText.length < 300 ? xhr.responseText : `Request failed (${xhr.status})`);
  });
  document.addEventListener("click", (e) => {
    const pick = e.target.closest("[data-pick-folder]");
    if (!pick) return;
    document.getElementById("folder").value = pick.dataset.pickFolder;
    document.getElementById("folder-picker").innerHTML = "";
  });
  setInterval(tickCountdowns, 1000);
})();
