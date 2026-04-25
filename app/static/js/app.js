// PriceWatch - client-side behavior
document.addEventListener('DOMContentLoaded', () => {
  // Submit city filter immediately on change when inside search sidebar form
  document.querySelectorAll('.pw-filters select').forEach(sel => {
    sel.addEventListener('change', () => sel.form?.submit());
  });

  // Live status polling
  initStatusPolling();
});

function initStatusPolling() {
  const badge = document.getElementById('pw-status');
  const text = document.getElementById('pw-status-text');
  if (!badge || !text) return;

  let wasRunning = false;

  async function fetchStatus() {
    try {
      const r = await fetch('/api/status', { cache: 'no-store' });
      if (!r.ok) throw new Error(r.status);
      const s = await r.json();
      updateBadge(badge, text, s);

      // Если был запущен сбор и завершился — перезагружаем страницу, чтобы показать свежие цены
      if (wasRunning && !s.running) {
        setTimeout(() => location.reload(), 1500);
      }
      wasRunning = s.running;
    } catch (e) {
      text.textContent = 'нет связи';
    }
  }

  function updateBadge(badge, text, s) {
    badge.classList.remove('refreshing', 'stale');
    if (s.running) {
      badge.classList.add('refreshing');
      text.textContent = 'собираем цены…';
      return;
    }
    if (!s.last_price_at) {
      badge.classList.add('stale');
      text.textContent = 'нет данных';
      return;
    }
    const ago = Math.round((Date.now() - new Date(s.last_price_at + 'Z').getTime()) / 60000);
    let label;
    if (ago < 1) label = 'только что';
    else if (ago < 60) label = `${ago} мин назад`;
    else if (ago < 1440) label = `${Math.floor(ago / 60)} ч назад`;
    else label = `${Math.floor(ago / 1440)} д назад`;
    if (ago > 120) badge.classList.add('stale');
    text.textContent = `обновлено ${label}`;
  }

  fetchStatus();
  setInterval(fetchStatus, 15000);
}

// Кнопка «обновить сейчас» для админки
async function triggerRefresh() {
  const btn = document.getElementById('pw-refresh-btn');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Запускаем…';
  }
  await fetch('/api/refresh', { method: 'POST' });
}
