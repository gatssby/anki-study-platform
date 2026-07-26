(function () {
  var STORAGE_KEY = "cronograma_scroll_restore";
  var UN_OPEN_MODULES_STORAGE_KEY = "cronograma_un_open_modules";

  function currentLocationKey() {
    return window.location.pathname + window.location.search;
  }

  function locationKeyFromUrl(value) {
    if (!value) {
      return currentLocationKey();
    }

    try {
      var parsed = new URL(value, window.location.origin);
      if (parsed.origin !== window.location.origin) {
        return currentLocationKey();
      }
      return parsed.pathname + parsed.search;
    } catch (_error) {
      return currentLocationKey();
    }
  }

  function formReturnLocationKey(form) {
    var returnInput = form.querySelector('input[name="return_to"]');
    return locationKeyFromUrl(returnInput ? returnInput.value : "");
  }

  function saveScrollPosition(path) {
    try {
      window.sessionStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({
          path: path || currentLocationKey(),
          scrollY: window.scrollY || window.pageYOffset || 0,
          savedAt: Date.now(),
        }),
      );
    } catch (_error) {
      // Ignore sessionStorage failures.
    }
  }

  function restoreScrollPosition() {
    try {
      var raw = window.sessionStorage.getItem(STORAGE_KEY);
      if (!raw) {
        return;
      }

      var payload = JSON.parse(raw);
      if (!payload || payload.path !== currentLocationKey()) {
        return;
      }

      if (typeof payload.savedAt === "number" && Date.now() - payload.savedAt > 15000) {
        window.sessionStorage.removeItem(STORAGE_KEY);
        return;
      }

      window.history.scrollRestoration = "manual";

      var targetY = Number(payload.scrollY) || 0;
      var scrollToClosestPosition = function () {
        var documentHeight = Math.max(
          document.documentElement ? document.documentElement.scrollHeight : 0,
          document.body ? document.body.scrollHeight : 0,
        );
        var maxScrollY = Math.max(0, documentHeight - (window.innerHeight || 0));
        window.scrollTo(0, Math.min(Math.max(0, targetY), maxScrollY));
      };
      window.requestAnimationFrame(function () {
        scrollToClosestPosition();
        window.setTimeout(function () {
          scrollToClosestPosition();
          window.sessionStorage.removeItem(STORAGE_KEY);
        }, 80);
      });
    } catch (_error) {
      // Ignore restore failures.
    }
  }

  function getUnModuleDetails() {
    return Array.prototype.slice.call(document.querySelectorAll("[data-un-module-details][data-un-module-id]"));
  }

  function hasUnModules() {
    return Boolean(document.querySelector("[data-un-modules]"));
  }

  function saveUnOpenModules(path) {
    if (!hasUnModules()) {
      return;
    }

    try {
      var openModuleIds = getUnModuleDetails()
        .filter(function (details) {
          return details.open;
        })
        .map(function (details) {
          return details.getAttribute("data-un-module-id");
        })
        .filter(Boolean);

      window.sessionStorage.setItem(
        UN_OPEN_MODULES_STORAGE_KEY,
        JSON.stringify({
          path: path || currentLocationKey(),
          openModuleIds: openModuleIds,
          savedAt: Date.now(),
        }),
      );
    } catch (_error) {
      // Ignore sessionStorage failures.
    }
  }

  function restoreUnOpenModules() {
    if (!hasUnModules()) {
      return;
    }

    try {
      var raw = window.sessionStorage.getItem(UN_OPEN_MODULES_STORAGE_KEY);
      if (!raw) {
        return;
      }

      var payload = JSON.parse(raw);
      if (!payload || payload.path !== currentLocationKey() || !Array.isArray(payload.openModuleIds)) {
        return;
      }

      if (typeof payload.savedAt === "number" && Date.now() - payload.savedAt > 12 * 60 * 60 * 1000) {
        window.sessionStorage.removeItem(UN_OPEN_MODULES_STORAGE_KEY);
        return;
      }

      var openModuleIds = payload.openModuleIds;
      getUnModuleDetails().forEach(function (details) {
        details.open = openModuleIds.indexOf(details.getAttribute("data-un-module-id")) !== -1;
      });
    } catch (_error) {
      // Ignore restore failures.
    }
  }

  function bindUnModuleState() {
    if (!hasUnModules()) {
      return;
    }

    document.addEventListener(
      "toggle",
      function (event) {
        if (event.target instanceof HTMLDetailsElement && event.target.matches("[data-un-module-details]")) {
          saveUnOpenModules();
        }
      },
      true,
    );

    document.addEventListener(
      "click",
      function (event) {
        var target = event.target;
        if (!(target instanceof Element)) {
          return;
        }

        var summary = target.closest("[data-un-module-details] summary");
        if (!summary) {
          return;
        }

        if (target.closest("button, input, select, textarea, label, a")) {
          var details = summary.closest("[data-un-module-details]");
          if (!details) {
            return;
          }
          var wasOpen = details.open;
          window.setTimeout(function () {
            details.open = wasOpen;
            saveUnOpenModules();
          }, 0);
        }
      },
      true,
    );
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) {
      return;
    }

    if (form.matches("[data-preserve-un-modules]") || form.closest("[data-un-module-details]")) {
      saveUnOpenModules(formReturnLocationKey(form));
    }

    if (
      form.method &&
      form.method.toLowerCase() === "post" &&
      form.matches("[data-preserve-scroll]")
    ) {
      saveScrollPosition(formReturnLocationKey(form));
    }
  });

  window.CronogramaScrollPreserve = {
    currentLocationKey: currentLocationKey,
    formReturnLocationKey: formReturnLocationKey,
    saveScrollPosition: saveScrollPosition,
    restoreScrollPosition: restoreScrollPosition,
    saveUnOpenModules: saveUnOpenModules,
    restoreUnOpenModules: restoreUnOpenModules,
    bindUnModuleState: bindUnModuleState,
  };

  function restoreUiState() {
    restoreUnOpenModules();
    restoreScrollPosition();
    bindUnModuleState();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", restoreUiState, { once: true });
  } else {
    restoreUiState();
  }
})();
