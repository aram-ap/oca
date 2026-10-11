// Animation control survives HTMX fragment swaps. No external dependencies.
(() => {
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let paused = reducedMotion.matches;
  const update = () => {
    document.documentElement.dataset.signalsPaused = String(paused);
    const button = document.querySelector('[data-toggle-signals]');
    if (button) {
      button.textContent = reducedMotion.matches ? 'Reduced motion' : paused ? 'Resume signals' : 'Pause signals';
      button.disabled = reducedMotion.matches;
      button.setAttribute('aria-pressed', String(paused));
    }
  };
  document.addEventListener('click', (event) => {
    if (event.target.closest('[data-toggle-signals]') && !reducedMotion.matches) { paused = !paused; update(); }
  });
  reducedMotion.addEventListener('change', (event) => { paused = event.matches; update(); });
  ['htmx:responseError', 'htmx:sendError', 'htmx:timeout'].forEach((name) => {
    document.addEventListener(name, (event) => {
      const element = event.detail.elt || event.detail.target;
      if (element && (element.id === 'system-map' || element.closest('#system-map'))) {
        const map = document.getElementById('system-map');
        if (map) map.dataset.stale = 'true';
      }
    });
  });
  update();
})();
