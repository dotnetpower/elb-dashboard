let activeDocsImageDialog = null;

function closeDocsImageDialog() {
  if (!activeDocsImageDialog) {
    return;
  }

  activeDocsImageDialog.remove();
  activeDocsImageDialog = null;
  document.documentElement.classList.remove("docs-image-fullscreen-open");
  document.body.classList.remove("docs-image-fullscreen-open");
}

function openDocsImageDialog(image) {
  const source = image.currentSrc || image.src;
  if (!source) {
    return;
  }

  closeDocsImageDialog();

  const dialog = document.createElement("div");
  dialog.className = "docs-image-fullscreen";
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");
  dialog.setAttribute("aria-label", image.alt || "Documentation image");

  const closeButton = document.createElement("button");
  closeButton.type = "button";
  closeButton.className = "docs-image-fullscreen-close";
  closeButton.textContent = "Close";
  closeButton.addEventListener("click", closeDocsImageDialog);

  const body = document.createElement("div");
  body.className = "docs-image-fullscreen-body";

  const fullscreenImage = document.createElement("img");
  fullscreenImage.src = source;
  fullscreenImage.alt = image.alt || "";
  body.appendChild(fullscreenImage);

  if (image.alt) {
    const caption = document.createElement("p");
    caption.className = "docs-image-fullscreen-caption";
    caption.textContent = image.alt;
    body.appendChild(caption);
  }

  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) {
      closeDocsImageDialog();
    }
  });

  dialog.append(closeButton, body);
  document.body.appendChild(dialog);
  document.documentElement.classList.add("docs-image-fullscreen-open");
  document.body.classList.add("docs-image-fullscreen-open");
  activeDocsImageDialog = dialog;
  closeButton.focus();
}

function attachDocsImageFullscreen(image) {
  if (image.dataset.fullscreenReady === "true" || image.closest("a")) {
    return;
  }

  image.dataset.fullscreenReady = "true";
  image.tabIndex = 0;
  image.setAttribute("role", "button");
  image.setAttribute("aria-label", `Open image fullscreen: ${image.alt || "documentation image"}`);
  image.addEventListener("click", () => openDocsImageDialog(image));
  image.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openDocsImageDialog(image);
    }
  });
}

function attachDocsImageViewer() {
  document.querySelectorAll("main .md-content img").forEach(attachDocsImageFullscreen);
}

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeDocsImageDialog();
  }
});

if (typeof document$ !== "undefined") {
  document$.subscribe(attachDocsImageViewer);
} else {
  document.addEventListener("DOMContentLoaded", attachDocsImageViewer);
}