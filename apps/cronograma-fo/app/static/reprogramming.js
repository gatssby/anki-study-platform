(function () {
  var root = document.querySelector("[data-reprogramming-app]");
  if (!root) {
    return;
  }

  var state = {
    today: root.getAttribute("data-today"),
    viewYear: Number(root.getAttribute("data-initial-year")),
    viewMonth: Number(root.getAttribute("data-initial-month")),
    settings: null,
    unavailability: [],
    settingsDirty: false,
    simulationToken: null,
    lastReport: null,
    selectedDate: null,
    loading: false,
    applying: false,
  };

  var monthNames = [
    "Janeiro",
    "Fevereiro",
    "Março",
    "Abril",
    "Maio",
    "Junho",
    "Julho",
    "Agosto",
    "Setembro",
    "Outubro",
    "Novembro",
    "Dezembro",
  ];

  var settingsForm = root.querySelector("[data-settings-form]");
  var examInput = root.querySelector('[data-setting-input="exam_date"]');
  var targetInput = root.querySelector('[data-setting-input="target_finish_date"]');
  var weekendsInput = root.querySelector('[data-setting-input="include_weekends"]');
  var saveButton = root.querySelector("[data-save-settings]");
  var simulateButton = root.querySelector("[data-run-simulation]");
  var applyButton = root.querySelector("[data-apply-simulation]");
  var calendarTitle = root.querySelector("[data-calendar-title]");
  var calendarGrid = root.querySelector("[data-calendar-grid]");
  var stateBadge = root.querySelector("[data-simulation-state]");
  var statusMessage = root.querySelector("[data-status-message]");
  var loadingPanel = root.querySelector("[data-loading-panel]");
  var loadingText = root.querySelector("[data-loading-text]");
  var summaryEmpty = root.querySelector("[data-summary-empty]");
  var summaryContent = root.querySelector("[data-summary-content]");
  var summaryMetrics = root.querySelector("[data-summary-metrics]");
  var firstDaysNode = root.querySelector("[data-first-days]");
  var lastDaysNode = root.querySelector("[data-last-days]");
  var weeklyNode = root.querySelector("[data-weekly-distribution]");
  var applySummary = root.querySelector("[data-apply-summary]");
  var applySummaryContent = root.querySelector("[data-apply-summary-content]");
  var modal = root.querySelector("[data-day-modal]");
  var modalTitle = root.querySelector("[data-modal-date-title]");
  var modalCard = modal.querySelector('[role="dialog"]');
  var modalTrigger = null;

  function modalFocusableElements() {
    return Array.from(
      modalCard.querySelectorAll('button:not([disabled]):not([hidden]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')
    ).filter(function (element) {
      return !element.hidden && element.getClientRects().length > 0;
    });
  }

  function api(url, options) {
    return fetch(url, options).then(function (response) {
      return response.json().catch(function () {
        return { ok: false, error: "Resposta inválida do servidor." };
      }).then(function (data) {
        if (!response.ok || !data.ok) {
          var message = data && data.error ? data.error : "Erro inesperado.";
          throw new Error(message);
        }
        return data;
      });
    });
  }

  function apiGet(url) {
    return api(url, { headers: { Accept: "application/json" } });
  }

  function apiPost(url, payload) {
    return api(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify(payload || {}),
    });
  }

  function apiDelete(url) {
    return api(url, {
      method: "DELETE",
      headers: { Accept: "application/json" },
    });
  }

  function toBrDate(isoDate) {
    if (!isoDate) {
      return "-";
    }
    var parts = isoDate.split("-");
    if (parts.length !== 3) {
      return isoDate;
    }
    return parts[2] + "/" + parts[1] + "/" + parts[0];
  }

  function formatUnits(units) {
    var total = Number(units || 0);
    if (!total) {
      return "0m";
    }
    var hours = Math.floor(total / 60);
    var minutes = total % 60;
    if (hours > 0) {
      return hours + "h " + String(minutes).padStart(2, "0") + "m";
    }
    return minutes + "m";
  }

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function isoForDate(year, month, day) {
    return [
      String(year).padStart(4, "0"),
      String(month).padStart(2, "0"),
      String(day).padStart(2, "0"),
    ].join("-");
  }

  function copySettingsFromInputs() {
    return {
      exam_date: examInput.value || null,
      target_finish_date: targetInput.value || null,
      include_weekends: weekendsInput.checked,
    };
  }

  function syncInputsFromSettings(settings) {
    examInput.value = settings.exam_date || "";
    targetInput.value = settings.target_finish_date || "";
    weekendsInput.checked = Boolean(settings.include_weekends);
  }

  function setStatus(mode, message) {
    stateBadge.textContent = mode;
    statusMessage.textContent = message;
  }

  function setLoading(active, message) {
    state.loading = active;
    if (loadingPanel) {
      loadingPanel.hidden = !active;
    }
    if (loadingText && message) {
      loadingText.textContent = message;
    }
    saveButton.disabled = active;
    simulateButton.disabled = active;
    updateApplyButton();
  }

  function markSimulationStale(message) {
    state.simulationToken = null;
    if (!state.applying) {
      setStatus("Simulação desatualizada", message || "As mudanças ainda não foram simuladas.");
    }
    updateApplyButton();
  }

  function updateApplyButton() {
    var reportIsValid = !state.lastReport || state.lastReport.feasible !== false;
    var canApply = !state.loading && !state.settingsDirty && Boolean(state.simulationToken) && reportIsValid;
    applyButton.disabled = !canApply;
  }

  function unavailableEntryForDate(isoDate) {
    return state.unavailability.find(function (entry) {
      return entry.start_date <= isoDate && entry.end_date >= isoDate;
    }) || null;
  }

  function dayBadges(isoDate) {
    var badges = [];
    if (state.settings && state.settings.exam_date === isoDate) {
      badges.push({ label: "Prova", cls: "badge--secondary" });
    }
    if (state.settings && state.settings.target_finish_date === isoDate) {
      badges.push({ label: "Fim aulas", cls: "badge--warning" });
    }
    if (unavailableEntryForDate(isoDate)) {
      badges.push({ label: "Indisponível", cls: "badge--danger" });
    }
    return badges;
  }

  function renderCalendar() {
    calendarTitle.textContent = monthNames[state.viewMonth - 1] + " " + state.viewYear;
    calendarGrid.innerHTML = "";

    var firstWeekday = new Date(state.viewYear, state.viewMonth - 1, 1).getDay();
    var normalizedFirstWeekday = firstWeekday === 0 ? 6 : firstWeekday - 1;
    var daysInMonth = new Date(state.viewYear, state.viewMonth, 0).getDate();

    for (var blank = 0; blank < normalizedFirstWeekday; blank += 1) {
      var blankCell = document.createElement("div");
      blankCell.className = "reprogramming-day reprogramming-day--blank";
      calendarGrid.appendChild(blankCell);
    }

    for (var day = 1; day <= daysInMonth; day += 1) {
      var isoDate = isoForDate(state.viewYear, state.viewMonth, day);
      var cell = document.createElement("button");
      cell.type = "button";
      cell.className = "reprogramming-day";
      if (isoDate === state.today) {
        cell.classList.add("reprogramming-day--today");
      }
      if (state.settings && state.settings.target_finish_date && isoDate > state.settings.target_finish_date) {
        cell.classList.add("reprogramming-day--after-target");
      }
      cell.setAttribute("data-date", isoDate);

      var badges = dayBadges(isoDate);
      cell.innerHTML =
        '<span class="reprogramming-day__number">' + day + "</span>" +
        '<span class="reprogramming-day__badges">' +
        badges.map(function (badge) {
          return '<span class="badge ' + badge.cls + '">' + badge.label + "</span>";
        }).join("") +
        "</span>";
      calendarGrid.appendChild(cell);
    }
  }

  function renderSummary() {
    if (!state.lastReport) {
      summaryEmpty.hidden = false;
      summaryContent.hidden = true;
      return;
    }

    summaryEmpty.hidden = true;
    summaryContent.hidden = false;
    summaryMetrics.innerHTML = [
      metricRow("Data da prova", toBrDate(state.lastReport.exam_date)),
      metricRow("Fim das aulas", toBrDate(state.lastReport.target_finish_date)),
      metricRow("Dias disponíveis", state.lastReport.days_available),
      metricRow("Dias indisponíveis", state.lastReport.days_unavailable),
      metricRow("Carga restante total", formatUnits(state.lastReport.remaining_total_units)),
      metricRow("Carga restante FO", formatUnits(state.lastReport.remaining_fo_units)),
      metricRow("Carga restante UN", formatUnits(state.lastReport.remaining_un_units)),
      metricRow("Aulas restantes FO", (state.lastReport.remaining_lessons_by_track || {}).FO || 0),
      metricRow("Aulas restantes UN", (state.lastReport.remaining_lessons_by_track || {}).UN || 0),
      metricRow("Aulas distribuídas FO", (state.lastReport.distributed_lessons_by_track || {}).FO || 0),
      metricRow("Aulas distribuídas UN", (state.lastReport.distributed_lessons_by_track || {}).UN || 0),
      metricRow("Média de aulas por dia", state.lastReport.average_lessons_per_day),
      metricRow("Média de minutos por dia", formatUnits(Math.round(state.lastReport.average_minutes_per_day))),
      metricRow("Maior carga diária", formatUnits(state.lastReport.max_daily_load_units)),
      metricRow("Menor carga diária", formatUnits(state.lastReport.min_daily_load_units)),
      metricRow("Dias acima do teto (informativo)", (state.lastReport.overflow_days || []).length),
      metricRow("Aulas pendentes", state.lastReport.pending_lessons),
      metricRow("Revisão/livre cortadas", state.lastReport.cut_review_free),
      metricRow("Inglês preservado", state.lastReport.cut_english_preserved),
    ].concat(trackDistributionMetrics(state.lastReport)).join("");

    firstDaysNode.innerHTML = buildDayList(state.lastReport.first_14_days);
    lastDaysNode.innerHTML = buildDayList(state.lastReport.last_14_days);
    weeklyNode.innerHTML = buildWeekList(state.lastReport.weekly_distribution);
  }

  function metricRow(label, value) {
    return (
      '<div class="reprogramming-metric">' +
      '<span class="console-inline-meta">' + escapeHtml(label) + "</span>" +
      "<strong>" + escapeHtml(value) + "</strong>" +
      "</div>"
    );
  }

  function trackDistributionMetrics(report) {
    var diagnostics = report.distribution_diagnostics || {};
    var tracks = diagnostics.tracks || {};
    var rows = [];
    ["FO", "UN"].forEach(function (trackCode) {
      var track = tracks[trackCode] || {};
      rows.push(metricRow(trackCode + " primeira data", toBrDate(track.first_date)));
      rows.push(metricRow(trackCode + " última data", toBrDate(track.last_date)));
      rows.push(metricRow(trackCode + " datas distintas", track.distinct_dates || 0));
      rows.push(metricRow(trackCode + " antes de hoje", track.before_today_count || 0));
    });
    if (report.validation_errors && report.validation_errors.length) {
      rows.push(metricRow("Validação", report.validation_errors.join("; ")));
    } else if (diagnostics.warnings && diagnostics.warnings.length) {
      rows.push(metricRow("Avisos", diagnostics.warnings.join("; ")));
    } else if (diagnostics.mode) {
      rows.push(metricRow("Validação", "FO e UN distribuídas"));
    }
    return rows;
  }

  function buildDayList(rows) {
    if (!rows || !rows.length) {
      return '<p class="muted">Sem datas geradas.</p>';
    }
    return (
      '<div class="reprogramming-list">' +
      rows.map(function (row) {
        return (
          '<article class="reprogramming-list__item">' +
          "<strong>" + toBrDate(row.date) + "</strong>" +
          "<span>" + row.lesson_count + " aula(s)</span>" +
          "<span>" + formatUnits(row.units) + "</span>" +
          "</article>"
        );
      }).join("") +
      "</div>"
    );
  }

  function buildWeekList(rows) {
    if (!rows || !rows.length) {
      return '<p class="muted">Sem semanas distribuídas.</p>';
    }
    return (
      '<div class="reprogramming-list">' +
      rows.map(function (row) {
        return (
          '<article class="reprogramming-list__item">' +
          "<strong>Semana de " + toBrDate(row.week_start) + "</strong>" +
          "<span>" + row.days + " dia(s)</span>" +
          "<span>" + formatUnits(row.units) + "</span>" +
          "</article>"
        );
      }).join("") +
      "</div>"
    );
  }

  function showApplySummary(report) {
    var diagnostics = report.distribution_diagnostics || {};
    var tracks = diagnostics.tracks || {};
    var fo = tracks.FO || {};
    var un = tracks.UN || {};
    applySummary.hidden = false;
    applySummaryContent.innerHTML =
      '<div class="reprogramming-list">' +
      '<article class="reprogramming-list__item"><strong>Backup criado</strong><span>' + escapeHtml(report.backup_path || "-") + "</span></article>" +
      '<article class="reprogramming-list__item"><strong>Aulas reprogramadas</strong><span>' + report.reprogrammed_lessons + "</span></article>" +
      '<article class="reprogramming-list__item"><strong>Carga redistribuída</strong><span>' + formatUnits(report.remaining_total_units) + "</span></article>" +
      '<article class="reprogramming-list__item"><strong>Média diária estimada (informativa)</strong><span>' + formatUnits(Math.round(report.daily_goal_units)) + "</span></article>" +
      '<article class="reprogramming-list__item"><strong>FO</strong><span>' + toBrDate(fo.first_date) + " a " + toBrDate(fo.last_date) + "</span><span>" + (fo.distinct_dates || 0) + " data(s)</span></article>" +
      '<article class="reprogramming-list__item"><strong>UN</strong><span>' + toBrDate(un.first_date) + " a " + toBrDate(un.last_date) + "</span><span>" + (un.distinct_dates || 0) + " data(s)</span></article>" +
      "</div>";
  }

  function hideApplySummary() {
    applySummary.hidden = true;
    applySummaryContent.innerHTML = "";
  }

  function openModal(isoDate, trigger) {
    state.selectedDate = isoDate;
    modalTrigger = trigger || document.activeElement;
    modal.hidden = false;
    document.body.style.overflow = "hidden";
    modalTitle.textContent = toBrDate(isoDate);
    var existing = unavailableEntryForDate(isoDate);
    root.querySelector('[data-modal-action="toggle-unavailable"]').hidden = Boolean(existing);
    root.querySelector('[data-modal-action="remove-unavailable"]').hidden = !existing;
    var focusable = modalFocusableElements();
    if (focusable.length) {
      focusable[0].focus();
    }
  }

  function closeModal() {
    modal.hidden = true;
    document.body.style.overflow = "";
    state.selectedDate = null;
    if (modalTrigger && document.contains(modalTrigger)) {
      modalTrigger.focus();
    }
    modalTrigger = null;
  }

  function saveSettings() {
    var payload = copySettingsFromInputs();
    return apiPost("/api/reprogramming/settings", payload).then(function (data) {
      state.settings = data.settings;
      state.settingsDirty = false;
      syncInputsFromSettings(state.settings);
      renderCalendar();
      markSimulationStale("Configurações salvas. Rode uma nova simulação.");
      return data.settings;
    });
  }

  function runSimulation() {
    var promise = state.settingsDirty ? saveSettings() : Promise.resolve();
    setLoading(true, "Calculando distribuição diária...");
    hideApplySummary();
    return promise
      .then(function () {
        return apiPost("/api/reprogramming/dry-run", {});
      })
      .then(function (data) {
        state.lastReport = data.report;
        state.simulationToken = data.report.simulation_token;
        renderSummary();
        if (data.report.feasible === false) {
          state.simulationToken = null;
          setStatus("Simulação com avisos", "Corrija os avisos antes de aplicar.");
        } else {
          setStatus("Simulação válida", "Dry-run concluído. Você já pode aplicar.");
        }
        updateApplyButton();
      })
      .catch(function (error) {
        state.lastReport = null;
        state.simulationToken = null;
        renderSummary();
        setStatus("Erro na simulação", error.message);
        window.alert(error.message);
      })
      .finally(function () {
        setLoading(false, "Calculando distribuição diária...");
      });
  }

  function applySimulation() {
    if (!state.simulationToken || state.settingsDirty) {
      return;
    }
    if (!window.confirm("Aplicar reprogramação global agora? Um backup automático será criado antes.")) {
      return;
    }
    state.applying = true;
    setLoading(true, "Aplicando reprogramação e criando backup...");
    apiPost("/api/reprogramming/apply", { simulation_token: state.simulationToken })
      .then(function (data) {
        state.lastReport = data.report;
        state.simulationToken = data.report.simulation_token;
        renderSummary();
        showApplySummary(data.report);
        setStatus("Aplicação concluída", "Cronograma atualizado com backup automático.");
      })
      .catch(function (error) {
        setStatus("Erro ao aplicar", error.message);
        window.alert(error.message);
      })
      .finally(function () {
        state.applying = false;
        setLoading(false, "Aplicando reprogramação e criando backup...");
      });
  }

  function refreshUnavailability() {
    return apiGet("/api/reprogramming/unavailability").then(function (data) {
      state.unavailability = data.items;
      renderCalendar();
    });
  }

  function initialize() {
    setLoading(true, "Carregando configuração...");
    Promise.all([
      apiGet("/api/reprogramming/settings"),
      apiGet("/api/reprogramming/unavailability"),
    ])
      .then(function (results) {
        state.settings = results[0].settings;
        state.unavailability = results[1].items;
        syncInputsFromSettings(state.settings);
        renderCalendar();
        renderSummary();
        setStatus("Configuração carregada", "Escolha as datas, simule e aplique quando estiver satisfeito.");
        updateApplyButton();
      })
      .catch(function (error) {
        setStatus("Erro ao carregar", error.message);
        window.alert(error.message);
      })
      .finally(function () {
        setLoading(false, "Carregando configuração...");
      });
  }

  settingsForm.addEventListener("input", function () {
    state.settingsDirty = true;
    hideApplySummary();
    markSimulationStale("Existem mudanças não simuladas.");
  });

  saveButton.addEventListener("click", function () {
    setLoading(true, "Salvando configurações...");
    hideApplySummary();
    saveSettings()
      .then(function () {
        setStatus("Configurações salvas", "Rode uma nova simulação para atualizar o resumo.");
      })
      .catch(function (error) {
        setStatus("Erro ao salvar", error.message);
        window.alert(error.message);
      })
      .finally(function () {
        setLoading(false, "Salvando configurações...");
      });
  });

  simulateButton.addEventListener("click", function () {
    runSimulation();
  });

  applyButton.addEventListener("click", function () {
    applySimulation();
  });

  root.querySelector("[data-prev-month]").addEventListener("click", function () {
    state.viewMonth -= 1;
    if (state.viewMonth < 1) {
      state.viewMonth = 12;
      state.viewYear -= 1;
    }
    renderCalendar();
  });

  root.querySelector("[data-next-month]").addEventListener("click", function () {
    state.viewMonth += 1;
    if (state.viewMonth > 12) {
      state.viewMonth = 1;
      state.viewYear += 1;
    }
    renderCalendar();
  });

  calendarGrid.addEventListener("click", function (event) {
    var target = event.target.closest("[data-date]");
    if (!target) {
      return;
    }
    openModal(target.getAttribute("data-date"), target);
  });

  modal.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      event.preventDefault();
      closeModal();
      return;
    }
    if (event.key !== "Tab") {
      return;
    }
    var focusable = modalFocusableElements();
    if (!focusable.length) {
      event.preventDefault();
      return;
    }
    var first = focusable[0];
    var last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });

  modal.addEventListener("click", function (event) {
    if (event.target.hasAttribute("data-close-modal")) {
      closeModal();
      return;
    }

    var action = event.target.getAttribute("data-modal-action");
    if (!action || !state.selectedDate) {
      return;
    }

    if (action === "set-exam") {
      examInput.value = state.selectedDate;
      state.settingsDirty = true;
      hideApplySummary();
      markSimulationStale("Data da prova alterada. Salve e simule novamente.");
      closeModal();
      return;
    }

    if (action === "set-target") {
      targetInput.value = state.selectedDate;
      state.settingsDirty = true;
      hideApplySummary();
      markSimulationStale("Data-alvo alterada. Salve e simule novamente.");
      closeModal();
      return;
    }

    if (action === "toggle-unavailable") {
      setLoading(true, "Salvando indisponibilidade...");
      apiPost("/api/reprogramming/unavailability", { date: state.selectedDate })
        .then(function () {
          return refreshUnavailability();
        })
        .then(function () {
          hideApplySummary();
          markSimulationStale("Indisponibilidade salva. Rode nova simulação.");
          closeModal();
        })
        .catch(function (error) {
          setStatus("Erro ao salvar", error.message);
          window.alert(error.message);
        })
        .finally(function () {
          setLoading(false, "Salvando indisponibilidade...");
        });
      return;
    }

    if (action === "remove-unavailable") {
      var entry = unavailableEntryForDate(state.selectedDate);
      if (!entry) {
        closeModal();
        return;
      }
      setLoading(true, "Removendo indisponibilidade...");
      apiDelete("/api/reprogramming/unavailability/" + entry.id)
        .then(function () {
          return refreshUnavailability();
        })
        .then(function () {
          hideApplySummary();
          markSimulationStale("Indisponibilidade removida. Rode nova simulação.");
          closeModal();
        })
        .catch(function (error) {
          setStatus("Erro ao remover", error.message);
          window.alert(error.message);
        })
        .finally(function () {
          setLoading(false, "Removendo indisponibilidade...");
        });
    }
  });

  initialize();
})();
