let activeMermaidDialog = null;

function closeMermaidDialog() {
  if (!activeMermaidDialog) {
    return;
  }

  activeMermaidDialog.remove();
  activeMermaidDialog = null;
  document.body.classList.remove("mermaid-fullscreen-open");
}

function openMermaidDialog(diagram) {
  const svg = diagram.querySelector("svg");
  if (!svg) {
    return;
  }

  closeMermaidDialog();

  const dialog = document.createElement("div");
  dialog.className = "mermaid-fullscreen";
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");
  dialog.setAttribute("aria-label", "Architecture diagram");

  const closeButton = document.createElement("button");
  closeButton.type = "button";
  closeButton.className = "mermaid-fullscreen-close";
  closeButton.textContent = "Close";
  closeButton.addEventListener("click", closeMermaidDialog);

  const body = document.createElement("div");
  body.className = "mermaid-fullscreen-body";
  body.appendChild(svg.cloneNode(true));

  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) {
      closeMermaidDialog();
    }
  });

  dialog.append(closeButton, body);
  document.body.appendChild(dialog);
  document.body.classList.add("mermaid-fullscreen-open");
  activeMermaidDialog = dialog;
  closeButton.focus();
}

function attachMermaidFullscreen(diagram) {
  if (diagram.dataset.fullscreenReady === "true") {
    return;
  }

  diagram.dataset.fullscreenReady = "true";
  diagram.tabIndex = 0;
  diagram.setAttribute("role", "button");
  diagram.setAttribute("aria-label", "Open architecture diagram fullscreen");
  diagram.addEventListener("click", () => openMermaidDialog(diagram));
  diagram.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openMermaidDialog(diagram);
    }
  });
}

function renderMermaid() {
  if (typeof mermaid === "undefined") {
    return;
  }

  const diagrams = Array.from(document.querySelectorAll(".mermaid"));
  if (diagrams.length === 0) {
    return;
  }

  mermaid.initialize({
    startOnLoad: false,
    securityLevel: "strict",
    theme: "base",
  });

  mermaid
    .run({ nodes: diagrams })
    .then(() => {
      diagrams.forEach((diagram) => {
        diagram.dataset.mermaidReady = "true";
        attachMermaidFullscreen(diagram);
      });
    })
    .catch((error) => {
      console.warn("Mermaid diagram rendering failed", error);
      diagrams.forEach((diagram) => {
        diagram.dataset.mermaidReady = "true";
      });
    });
}

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeMermaidDialog();
  }
});

if (typeof document$ !== "undefined") {
  document$.subscribe(renderMermaid);
} else {
  document.addEventListener("DOMContentLoaded", renderMermaid);
}