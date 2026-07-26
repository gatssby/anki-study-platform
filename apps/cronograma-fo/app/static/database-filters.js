(function () {
  var form = document.querySelector("[data-filter-form]");
  if (!form) {
    return;
  }

  function inputFor(name) {
    return form.querySelector('[data-filter-input="' + name + '"]');
  }

  function setValue(name, value) {
    var input = inputFor(name);
    if (input) {
      input.value = value;
    }
  }

  function setActive(groupName, value) {
    form.querySelectorAll('[data-chip-group="' + groupName + '"] [data-filter-value]').forEach(function (chip) {
      var isActive = chip.getAttribute("data-filter-value") === value;
      chip.classList.toggle("active", isActive);
      chip.setAttribute("aria-pressed", isActive ? "true" : "false");
    });
  }

  function updateDependentPanels(track) {
    var activeTrack = track || "all";
    form.setAttribute("data-track-state", activeTrack);

    var dependentRow = form.querySelector("[data-dependent-row]");
    var subjectPanel = form.querySelector('[data-dependent-panel="subject"]');
    var subjectRow = form.querySelector('[data-chip-group="subject"]');
    var frontPanel = form.querySelector('[data-dependent-panel="front"]');
    var frontRow = form.querySelector('[data-chip-group="front"]');

    var subjectEnabled = activeTrack === "FO" || activeTrack === "UN";
    if (subjectPanel) {
      subjectPanel.classList.toggle("is-collapsed", !subjectEnabled);
    }
    if (subjectRow) {
      subjectRow.classList.toggle("is-hidden", !subjectEnabled);
      subjectRow.querySelectorAll("[data-subject-track]").forEach(function (chip) {
        chip.classList.toggle("is-hidden", chip.getAttribute("data-subject-track") !== activeTrack);
      });
    }

    var frontEnabled = activeTrack === "FO";
    if (frontPanel) {
      frontPanel.classList.toggle("is-collapsed", !frontEnabled);
    }
    if (frontRow) {
      frontRow.classList.toggle("is-hidden", !frontEnabled);
    }

    if (dependentRow) {
      dependentRow.classList.toggle("is-collapsed", !subjectEnabled && !frontEnabled);
    }
  }

  form.querySelectorAll("[data-filter-target]").forEach(function (chip) {
    chip.addEventListener("click", function () {
      var target = chip.getAttribute("data-filter-target");
      var value = chip.getAttribute("data-filter-value") || "all";

      if (target === "track") {
        var currentTrack = (inputFor("track") && inputFor("track").value) || "all";
        if (currentTrack !== value) {
          setValue("subject", "all");
          setValue("front", "all");
          setActive("subject", "all");
          setActive("front", "all");
        }
        updateDependentPanels(value);
      }

      setValue(target, value);
      setActive(target, value);
    });
  });

  updateDependentPanels((inputFor("track") && inputFor("track").value) || "all");
})();
