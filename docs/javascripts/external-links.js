function openExternalLinksInNewTabs() {
  const currentHost = window.location.host;

  document.querySelectorAll('main a[href^="http://"], main a[href^="https://"]').forEach((link) => {
    let url;

    try {
      url = new URL(link.href);
    } catch {
      return;
    }

    if (url.host === currentHost) {
      return;
    }

    link.target = "_blank";
    link.rel = "noopener noreferrer";
  });
}

if (typeof document$ !== "undefined") {
  document$.subscribe(openExternalLinksInNewTabs);
} else {
  document.addEventListener("DOMContentLoaded", openExternalLinksInNewTabs);
}