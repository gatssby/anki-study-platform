(function () {
  function toIsoDate(brValue) {
    var match = /^(\d{2})\/(\d{2})\/(\d{4})$/.exec((brValue || "").trim());
    if (!match) {
      return null;
    }

    var day = Number(match[1]);
    var month = Number(match[2]);
    var year = Number(match[3]);
    var candidate = new Date(year, month - 1, day);

    if (
      candidate.getFullYear() !== year ||
      candidate.getMonth() !== month - 1 ||
      candidate.getDate() !== day
    ) {
      return null;
    }

    var isoMonth = String(month).padStart(2, "0");
    var isoDay = String(day).padStart(2, "0");
    return year + "-" + isoMonth + "-" + isoDay;
  }

  function toBrDate(isoValue) {
    var match = /^(\d{4})-(\d{2})-(\d{2})$/.exec((isoValue || "").trim());
    if (!match) {
      return "";
    }
    return match[3] + "/" + match[2] + "/" + match[1];
  }

  function formatAsBr(value) {
    var digits = (value || "").replace(/\D/g, "").slice(0, 8);
    if (digits.length <= 2) {
      return digits;
    }
    if (digits.length <= 4) {
      return digits.slice(0, 2) + "/" + digits.slice(2);
    }
    return digits.slice(0, 2) + "/" + digits.slice(2, 4) + "/" + digits.slice(4);
  }

  var forms = document.querySelectorAll("[data-date-form]");
  if (!forms.length) {
    return;
  }

  forms.forEach(function (form) {
    var displayInput = form.querySelector("[data-date-display]");
    var isoInput = form.querySelector("[data-date-iso]");
    var pickerInput = form.querySelector("[data-date-picker]");
    var errorNode = form.querySelector("[data-date-error]");

    if (!displayInput || !isoInput) {
      return;
    }

    function hideError() {
      if (errorNode) {
        errorNode.hidden = true;
      }
    }

    function syncFromIso(isoValue) {
      var brValue = toBrDate(isoValue);
      if (!brValue) {
        return;
      }

      displayInput.value = brValue;
      isoInput.value = isoValue;
      if (pickerInput && pickerInput.value !== isoValue) {
        pickerInput.value = isoValue;
      }
      hideError();
    }

    syncFromIso(isoInput.value);

    displayInput.addEventListener("input", function () {
      var formatted = formatAsBr(displayInput.value);
      displayInput.value = formatted;
      hideError();

      var isoValue = toIsoDate(formatted);
      if (isoValue) {
        isoInput.value = isoValue;
        if (pickerInput) {
          pickerInput.value = isoValue;
        }
      }
    });

    if (pickerInput) {
      pickerInput.addEventListener("change", function () {
        syncFromIso(pickerInput.value);
      });
    }

    form.addEventListener("submit", function (event) {
      var isoValue = toIsoDate(displayInput.value);
      if (!isoValue) {
        event.preventDefault();
        if (errorNode) {
          errorNode.hidden = false;
        }
        displayInput.focus();
        return;
      }

      isoInput.value = isoValue;
      if (pickerInput) {
        pickerInput.value = isoValue;
      }
      hideError();
    });
  });
})();
