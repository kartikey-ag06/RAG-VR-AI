// vr-shell.js - shared top navigation for the VR_RAG web pages + a live server status pill + window.vrToast().
(function () {
  const PAGES = [
    ['/upload', '📄', 'Documents'], ['/workspace', '🗂', 'Workspace'], ['/procops', '📈', 'ProcOps'],
    ['/expert', '🎧', 'Expert'], ['/admin', '⚙', 'Admin'],
  ];
  const here = location.pathname.replace(/\/$/, '') || '/';
  const logo = '<svg width="30" height="18" viewBox="0 0 60 36" fill="none"><defs><linearGradient id="vrg" x1="0" x2="1">' +
    '<stop offset="0" stop-color="#4fb3ff"/><stop offset="1" stop-color="#8b6cff"/></linearGradient></defs>' +
    '<rect x="3" y="6" width="22" height="20" rx="8" stroke="url(#vrg)" stroke-width="4"/>' +
    '<rect x="35" y="6" width="22" height="20" rx="8" stroke="url(#vrg)" stroke-width="4"/>' +
    '<path d="M25 14h10" stroke="url(#vrg)" stroke-width="4" stroke-linecap="round"/></svg>';
  const nav = document.createElement('nav');
  nav.className = 'vr-nav';
  nav.innerHTML = `<a class="vr-brand" href="/upload">${logo}<span>VR_RAG <small>· AI glasses console</small></span></a>
    <div class="vr-links">${PAGES.map(([u, i, t]) => `<a href="${u}" class="${here === u ? 'on' : ''}">${i} ${t}</a>`).join('')}</div>
    <span class="vr-status" title="Server status"><i class="vr-dot"></i><span id="vr-st">connecting…</span></span>
    <a class="btn primary vr-launch" href="/" target="_blank" rel="noopener">🥽 Open glasses</a>`;
  document.body.prepend(nav);

  async function status() {
    const dot = nav.querySelector('.vr-dot'), el = nav.querySelector('#vr-st');
    try {
      const s = await (await fetch('/api/grok/status', { cache: 'no-store' })).json();
      dot.classList.remove('off');
      el.textContent = `${s.docs} docs · ${s.provider || 'no AI'}${s.model ? ' ' + s.model.split('/').pop() : ''} · ${s.storage === 'postgres+pgvector' ? 'Neon pgvector' : 'local'} · ${s.search}`;
    } catch { dot.classList.add('off'); el.textContent = 'server offline'; }
  }
  status(); setInterval(() => { if (!document.hidden) status(); }, 60000);

  let box;
  window.vrToast = function (text, kind = 'info', ms = 3500) {
    if (!box) { box = document.createElement('div'); box.className = 'vr-toasts'; document.body.appendChild(box); }
    const d = document.createElement('div'); d.className = 'vr-toast ' + kind; d.textContent = text;
    box.appendChild(d); setTimeout(() => d.remove(), ms);
  };
})();
