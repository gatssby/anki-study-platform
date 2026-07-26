(function () {
  const form = document.querySelector("[data-review-question-form]");
  if (!form) {
    return;
  }

  const maxBytes = Number(form.dataset.maxImageBytes || "10485760");
  const acceptedMimeTypes = new Set(
    String(form.dataset.acceptedMimeTypes || "")
      .split(",")
      .map((value) => value.trim().toLowerCase())
      .filter(Boolean),
  );
  const acceptedExtensions = new Set(["png", "jpg", "jpeg", "webp"]);
  const widgets = Array.from(form.querySelectorAll("[data-upload-widget]"));
  const objectiveToggle = form.querySelector("[data-objective-toggle]");
  const objectiveOnly = form.querySelector("[data-objective-only]");
  const objectiveSelect = form.querySelector("[data-objective-select]");
  const existingQuestionImages = Array.from(form.querySelectorAll("[data-existing-question-image]"));

  let activeWidget = widgets[0] || null;

  function isAcceptedFile(file) {
    const fileName = String(file.name || "");
    const extension = fileName.includes(".") ? fileName.split(".").pop().toLowerCase() : "";
    const type = String(file.type || "").toLowerCase();
    if (!acceptedExtensions.has(extension)) {
      return "Formato de imagem inválido. Use png, jpg, jpeg ou webp.";
    }
    if (type && !acceptedMimeTypes.has(type)) {
      return "Tipo de arquivo inválido. Use png, jpg, jpeg ou webp.";
    }
    if (file.size > maxBytes) {
      return "Imagem excede o limite de 10 MB.";
    }
    return null;
  }

  function assignFileToInput(input, file) {
    const transfer = new DataTransfer();
    transfer.items.add(file);
    input.files = transfer.files;
  }

  function assignFilesToInput(input, files) {
    const transfer = new DataTransfer();
    files.forEach((file) => transfer.items.add(file));
    input.files = transfer.files;
  }

  function getWidgetParts(widget) {
    return {
      input: widget.querySelector("[data-file-input]"),
      preview: widget.querySelector("[data-preview]"),
      previewList: widget.querySelector("[data-preview-list]"),
      previewGrid: widget.querySelector("[data-preview-grid]"),
      previewImage: widget.querySelector("[data-preview-image]"),
      previewName: widget.querySelector("[data-preview-name]"),
      previewSummary: widget.querySelector("[data-preview-summary]"),
      error: widget.querySelector("[data-upload-error]"),
    };
  }

  function setWidgetError(widget, message) {
    const { error } = getWidgetParts(widget);
    if (!error) {
      return;
    }
    error.textContent = message || "";
    error.hidden = !message;
  }

  function clearPreview(widget) {
    const { input, preview, previewList, previewGrid, previewImage, previewName, previewSummary } = getWidgetParts(widget);
    previewGrid?.querySelectorAll("[data-object-url]").forEach((node) => {
      URL.revokeObjectURL(node.dataset.objectUrl);
    });
    if (previewImage && previewImage.dataset.objectUrl) {
      URL.revokeObjectURL(previewImage.dataset.objectUrl);
      delete previewImage.dataset.objectUrl;
    }
    if (input) {
      input.value = "";
    }
    if (previewImage) {
      previewImage.removeAttribute("src");
    }
    if (previewName) {
      previewName.textContent = "";
    }
    if (previewSummary) {
      previewSummary.textContent = "";
    }
    if (preview) {
      preview.hidden = true;
    }
    if (previewList) {
      previewList.hidden = true;
    }
    if (previewGrid) {
      previewGrid.replaceChildren();
    }
    setWidgetError(widget, "");
  }

  function renderPreview(widget, file) {
    const { preview, previewImage, previewName } = getWidgetParts(widget);
    if (!preview || !previewImage || !previewName) {
      return;
    }

    if (previewImage.dataset.objectUrl) {
      URL.revokeObjectURL(previewImage.dataset.objectUrl);
    }
    const objectUrl = URL.createObjectURL(file);
    previewImage.src = objectUrl;
    previewImage.dataset.objectUrl = objectUrl;
    previewName.textContent = `${file.name} · ${Math.max(1, Math.round(file.size / 1024))} KB`;
    preview.hidden = false;
  }

  function renderPreviewList(widget, files) {
    const { previewList, previewGrid, previewSummary } = getWidgetParts(widget);
    if (!previewList || !previewGrid || !previewSummary) {
      return;
    }

    previewGrid.replaceChildren();
    files.forEach((file, index) => {
      const item = document.createElement("figure");
      item.className = "review-upload-widget__preview-item";

      const image = document.createElement("img");
      image.alt = `Preview da imagem da questão ${index + 1}`;
      const objectUrl = URL.createObjectURL(file);
      image.src = objectUrl;
      image.dataset.objectUrl = objectUrl;
      item.appendChild(image);

      const caption = document.createElement("figcaption");
      caption.textContent = `${file.name} · ${Math.max(1, Math.round(file.size / 1024))} KB`;
      item.appendChild(caption);
      previewGrid.appendChild(item);
    });

    previewSummary.textContent = `${files.length} imagem${files.length === 1 ? "" : "ns"} selecionada${files.length === 1 ? "" : "s"}`;
    previewList.hidden = false;
  }

  function handleSelectedFile(widget, file) {
    if (!file) {
      clearPreview(widget);
      return false;
    }
    const validationError = isAcceptedFile(file);
    if (validationError) {
      clearPreview(widget);
      setWidgetError(widget, validationError);
      return false;
    }
    const { input } = getWidgetParts(widget);
    if (input) {
      assignFileToInput(input, file);
    }
    setWidgetError(widget, "");
    renderPreview(widget, file);
    return true;
  }

  function handleSelectedFiles(widget, files) {
    const validFiles = [];
    for (const file of files) {
      const validationError = isAcceptedFile(file);
      if (validationError) {
        clearPreview(widget);
        setWidgetError(widget, validationError);
        return false;
      }
      validFiles.push(file);
    }
    const { input } = getWidgetParts(widget);
    if (input) {
      assignFilesToInput(input, validFiles);
    }
    setWidgetError(widget, "");
    renderPreviewList(widget, validFiles);
    return true;
  }

  function resolveTargetWidget() {
    const emptyWidget = widgets.find((widget) => {
      const { input } = getWidgetParts(widget);
      return input && (!input.files || input.files.length === 0);
    });
    return activeWidget || emptyWidget || widgets[0] || null;
  }

  function syncObjectiveState() {
    const isObjective = Boolean(objectiveToggle && objectiveToggle.checked);
    if (objectiveOnly) {
      objectiveOnly.hidden = !isObjective;
    }
    if (objectiveSelect) {
      objectiveSelect.disabled = !isObjective;
      if (!isObjective) {
        objectiveSelect.value = "";
      }
    }
  }

  function syncExistingImageRemoval(card, isMarkedForRemoval) {
    const hiddenInput = card.querySelector("[data-remove-image-input]");
    const button = card.querySelector("[data-remove-image-button]");
    const status = card.querySelector("[data-remove-image-status]");

    card.classList.toggle("is-marked-for-removal", isMarkedForRemoval);
    if (hiddenInput) {
      hiddenInput.disabled = !isMarkedForRemoval;
    }
    if (button) {
      button.setAttribute("aria-pressed", isMarkedForRemoval ? "true" : "false");
      button.setAttribute(
        "aria-label",
        isMarkedForRemoval ? "Desfazer remoção da imagem atual da questão" : "Remover imagem atual da questão",
      );
      button.title = isMarkedForRemoval ? "Desfazer remoção" : "Remover imagem";
    }
    if (status) {
      status.textContent = isMarkedForRemoval ? "Será removida ao salvar" : "Imagem atual";
    }
  }

  widgets.forEach((widget) => {
    const { input } = getWidgetParts(widget);
    const trigger = widget.querySelector("[data-file-trigger]");
    const clearButton = widget.querySelector("[data-clear-upload]");
    const activate = () => {
      activeWidget = widget;
      widgets.forEach((entry) => entry.classList.toggle("is-active", entry === widget));
    };

    widget.addEventListener("focusin", activate);
    widget.addEventListener("click", activate);
    trigger?.addEventListener("click", () => {
      activate();
      input?.click();
    });
    input?.addEventListener("change", () => {
      const selectedFiles = Array.from(input.files || []);
      if (input.multiple) {
        handleSelectedFiles(widget, selectedFiles);
        return;
      }
      const file = selectedFiles[0] || null;
      handleSelectedFile(widget, file);
    });
    clearButton?.addEventListener("click", () => {
      clearPreview(widget);
      activate();
    });
  });

  existingQuestionImages.forEach((card) => {
    const button = card.querySelector("[data-remove-image-button]");
    const hiddenInput = card.querySelector("[data-remove-image-input]");
    syncExistingImageRemoval(card, Boolean(hiddenInput && !hiddenInput.disabled));
    button?.addEventListener("click", () => {
      const nextValue = !(hiddenInput && !hiddenInput.disabled);
      syncExistingImageRemoval(card, nextValue);
    });
  });

  form.addEventListener("paste", async (event) => {
    const clipboardItems = Array.from(event.clipboardData?.items || []);
    const imageItem = clipboardItems.find((item) => String(item.type || "").startsWith("image/"));
    if (!imageItem) {
      return;
    }
    const targetWidget = resolveTargetWidget();
    if (!targetWidget) {
      return;
    }

    const file = imageItem.getAsFile();
    if (!file) {
      return;
    }

    event.preventDefault();
    const extension = file.type === "image/png"
      ? "png"
      : file.type === "image/webp"
        ? "webp"
        : "jpg";
    const pastedFile = new File([file], `pasted-image.${extension}`, { type: file.type || "image/png" });
    const { input } = getWidgetParts(targetWidget);
    if (input && input.multiple) {
      const currentFiles = Array.from(input.files || []);
      handleSelectedFiles(targetWidget, [...currentFiles, pastedFile]);
      return;
    }
    handleSelectedFile(targetWidget, pastedFile);
  });

  form.addEventListener("submit", (event) => {
    const firstInvalidWidget = widgets.find((widget) => {
      const { input } = getWidgetParts(widget);
      if (!input || !input.files || input.files.length === 0) {
        return false;
      }
      for (const file of Array.from(input.files)) {
        const validationError = isAcceptedFile(file);
        if (validationError) {
          setWidgetError(widget, validationError);
          return true;
        }
      }
      return false;
    });

    if (firstInvalidWidget) {
      event.preventDefault();
      activeWidget = firstInvalidWidget;
      firstInvalidWidget.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  });

  objectiveToggle?.addEventListener("change", syncObjectiveState);
  syncObjectiveState();
  if (activeWidget) {
    activeWidget.classList.add("is-active");
  }
})();
