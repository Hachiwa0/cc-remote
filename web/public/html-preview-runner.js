(() => {
  // The response CSP intentionally permits user-requested inline scripts.
  // Refuse to initialize unless the parent supplied an opaque-origin iframe
  // (sandbox="allow-scripts" without allow-same-origin).
  try {
    void parent.document;
    return;
  } catch {
    // Expected SecurityError: this is the required isolation boundary.
  }
  const receive = (event) => {
    if (event.source !== parent
        || !event.data
        || event.data.type !== "cc-remote-html-preview"
        || typeof event.data.document !== "string") return;
    window.removeEventListener("message", receive);
    document.open();
    document.write(event.data.document);
    document.close();
  };
  window.addEventListener("message", receive);
})();
