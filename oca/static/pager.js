// Client-side paging for long lists (ticket event log, ticket timeline). Rows stay in the DOM so live
// SSE appends and htmx swaps keep working; the pager only toggles visibility.
(() => {
  const SIZES = [25, 50, 100, 0];  // 0 = all
  const label = (n) => (n === 0 ? 'All' : String(n));

  function setup(root) {
    if (root.dataset.pagerReady) return;
    root.dataset.pagerReady = 'true';
    const key = 'oca.pager.' + (root.dataset.pagerKey || 'default');
    const controls = root.querySelector('[data-pager-controls]');
    let size = 50;
    try { const saved = parseInt(localStorage.getItem(key) || '', 10); if (!Number.isNaN(saved) && SIZES.includes(saved)) size = saved; } catch (e) {}
    let page = 1;
    let follow = root.dataset.pagerStart === 'last';  // keep showing the newest page as rows arrive

    controls.innerHTML = '<label>Rows per page <select data-size></select></label>'
      + '<button type="button" class="outline" data-prev>&larr; Newer</button>'
      + '<span data-status></span>'
      + '<button type="button" class="outline" data-next>Older &rarr;</button>';
    const select = controls.querySelector('[data-size]');
    SIZES.forEach((n) => { const o = document.createElement('option'); o.value = n; o.textContent = label(n); select.appendChild(o); });
    select.value = String(size);
    const prev = controls.querySelector('[data-prev]'), next = controls.querySelector('[data-next]'), status = controls.querySelector('[data-status]');
    if (root.dataset.pagerStart === 'last') { prev.innerHTML = '&larr; Earlier'; next.innerHTML = 'Later &rarr;'; }

    function rows() { return Array.from(root.querySelectorAll(root.dataset.pager)); }
    function render() {
      const all = rows(), total = all.length;
      const pages = size === 0 ? 1 : Math.max(1, Math.ceil(total / size));
      if (follow) page = pages;
      page = Math.min(Math.max(1, page), pages);
      all.forEach((row, i) => {
        const visible = size === 0 || (i >= (page - 1) * size && i < page * size);
        row.hidden = !visible;
      });
      const from = total === 0 ? 0 : (size === 0 ? 1 : (page - 1) * size + 1);
      const to = size === 0 ? total : Math.min(total, page * size);
      status.textContent = total === 0 ? 'No entries' : `${from}–${to} of ${total} · page ${page}/${pages}`;
      prev.disabled = page <= 1; next.disabled = page >= pages;
    }
    select.addEventListener('change', () => { size = parseInt(select.value, 10); try { localStorage.setItem(key, String(size)); } catch (e) {} render(); });
    prev.addEventListener('click', () => { follow = false; page -= 1; render(); });
    next.addEventListener('click', () => { page += 1; render(); const pages = size === 0 ? 1 : Math.ceil(rows().length / size); follow = root.dataset.pagerStart === 'last' && page >= pages; });
    new MutationObserver(() => render()).observe(root, { childList: true, subtree: true });
    render();
  }

  function init() { document.querySelectorAll('[data-pager]').forEach(setup); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
  document.addEventListener('htmx:afterSettle', init);
})();
