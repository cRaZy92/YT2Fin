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

  // A live refresh replaces the whole list; hold it while the user is dragging or editing a row,
  // and catch up as soon as they finish.
  let dragging = false;

  function refreshIfStale() {
    const list = document.getElementById("video-list");
    if (!list || !list.dataset.stale || dragging || list.querySelector(".editing")) return;
    delete list.dataset.stale;
    htmx.ajax("GET", list.getAttribute("hx-get"), { target: "#video-list", swap: "outerHTML" });
  }

  function initSortables() {
    document.querySelectorAll("[data-sortable]").forEach((list) => {
      if (list._sortable) return;
      list._sortable = Sortable.create(list, {
        handle: ".drag",
        draggable: "[data-video-id]",
        animation: 150,
        onStart() { dragging = true; },
        onEnd() {
          dragging = false;
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

  // "Show deleted" on a mapping's list: the list is re-rendered on every change, so keep the choice here.
  function storedShowDeleted() {
    try { return localStorage.getItem("showDeleted") !== "0"; } catch { return true; }
  }

  function initShowDeleted() {
    const list = document.getElementById("video-list");
    const toggle = list && list.querySelector("[data-show-deleted]");
    if (!toggle) return;
    toggle.checked = storedShowDeleted();
    list.classList.toggle("show-deleted", toggle.checked);
    toggle.onchange = () => {
      list.classList.toggle("show-deleted", toggle.checked);
      try { localStorage.setItem("showDeleted", toggle.checked ? "1" : "0"); } catch { /* private mode */ }
    };
  }

  function refresh() { tickCountdowns(); initSortables(); followLog(); initShowDeleted(); }

  document.addEventListener("DOMContentLoaded", () => { refresh(); toggleTypeFields(); });
  document.addEventListener("htmx:afterSettle", () => { refresh(); refreshIfStale(); });
  document.addEventListener("htmx:beforeRequest", (e) => {
    const elt = e.detail.elt;
    if (elt.id !== "video-list" || e.detail.requestConfig.verb !== "get") return;
    if (dragging || elt.querySelector(".editing")) {
      e.preventDefault();
      elt.dataset.stale = "1";
    }
  });
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
