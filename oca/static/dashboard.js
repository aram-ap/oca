// Local CSS 3D camera; state persists across live HTMX map updates.
(() => {
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let paused = reducedMotion.matches;
  const camera = { threeD: true, tilt: 40, rotation: -12, zoom: 1 };
  let observer;
  let drag;
  const update = () => {
    document.documentElement.dataset.signalsPaused = String(paused);
    document.documentElement.dataset.mapView = camera.threeD ? '3d' : '2d';
    const button = document.querySelector('[data-toggle-signals]');
    if (button) {
      button.textContent = reducedMotion.matches ? 'Reduced motion' : paused ? 'Resume signals' : 'Pause signals';
      button.disabled = reducedMotion.matches;
      button.setAttribute('aria-pressed', String(paused));
    }
    const view = document.querySelector('button[data-map-view]');
    if (view) { view.textContent = camera.threeD ? '3D view' : '2D view'; view.setAttribute('aria-pressed', String(camera.threeD)); }
    const stage = document.querySelector('.map-camera');
    const world = document.querySelector('.map-world');
    if (stage && world && stage.clientWidth > 0 && stage.clientHeight > 0) {
      const fit = Math.min(stage.clientWidth / 1660, stage.clientHeight / 1230);
      world.style.transform = `translate(-50%, -50%) scale(${fit * camera.zoom}) rotateX(${camera.threeD ? camera.tilt : 0}deg) rotateZ(${camera.threeD ? camera.rotation : 0}deg)`;
    }
  };
  const bindCamera = () => {
    if (observer) observer.disconnect();
    const stage = document.querySelector('.map-camera');
    if (stage && typeof ResizeObserver !== 'undefined') { observer = new ResizeObserver(update); observer.observe(stage); }
    update();
  };
  document.addEventListener('click', (event) => {
    if (event.target.closest('[data-toggle-signals]') && !reducedMotion.matches) { paused = !paused; update(); }
    if (event.target.closest('button[data-map-view]')) { camera.threeD = !camera.threeD; update(); }
    const control = event.target.closest('[data-camera-action]');
    if (!control) return;
    const action = control.dataset.cameraAction;
    if (action === 'left') camera.rotation -= 10;
    if (action === 'right') camera.rotation += 10;
    if (action === 'in') camera.zoom = Math.min(1.8, camera.zoom + 0.15);
    if (action === 'out') camera.zoom = Math.max(0.65, camera.zoom - 0.15);
    if (action === 'reset') Object.assign(camera, { threeD: true, tilt: 40, rotation: -12, zoom: 1 });
    update();
  });
  document.addEventListener('pointerdown', (event) => {
    const stage = event.target.closest('.map-camera');
    if (!stage || event.target.closest('a') || !camera.threeD || event.button !== 0) return;
    drag = { x: event.clientX, y: event.clientY, rotation: camera.rotation, tilt: camera.tilt, stage, id: event.pointerId };
    stage.setPointerCapture(event.pointerId);
    stage.classList.add('dragging');
  });
  document.addEventListener('pointermove', (event) => {
    if (!drag || drag.id !== event.pointerId) return;
    camera.rotation = drag.rotation + (event.clientX - drag.x) * 0.15;
    camera.tilt = Math.max(10, Math.min(60, drag.tilt - (event.clientY - drag.y) * 0.12));
    update();
  });
  const endDrag = () => { if (drag) drag.stage.classList.remove('dragging'); drag = null; };
  document.addEventListener('pointerup', endDrag);
  document.addEventListener('pointercancel', endDrag);
  document.addEventListener('htmx:beforeSwap', (event) => { if (event.detail.target.id === 'system-map') endDrag(); });
  document.addEventListener('htmx:afterSwap', bindCamera);
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
  window.addEventListener('resize', update);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bindCamera, { once: true });
  else bindCamera();
})();
