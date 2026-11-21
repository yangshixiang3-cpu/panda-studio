// 外框交互：左侧导航高亮、右栏页签滑动指示条。不依赖 app.js，只读写 DOM 状态。
const nav = [...document.querySelectorAll('.rail [data-nav]')];

function currentPage() {
  const hash = location.hash;
  if (hash === '#refs') return 'refs';
  if (hash === '#settings') return 'settings';
  if (hash.startsWith('#task/')) return 'task';
  return 'home';
}

function syncNav() {
  const page = currentPage();
  for (const link of nav) {
    const active = link.dataset.nav === page || (page === 'task' && link.dataset.nav === 'home');
    if (active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
}
window.addEventListener('hashchange', syncNav);
syncNav();

// 页签指示条：app.js 切换 aria-selected，这里只同步一个 CSS 变量（只在切换时触发，不常驻）
const tablist = document.querySelector('.adjustments .tabs');
if (tablist) {
  const tabs = [...tablist.querySelectorAll('[role=tab]')];
  const syncTabs = () => {
    const index = Math.max(0, tabs.findIndex(tab => tab.getAttribute('aria-selected') === 'true'));
    tablist.style.setProperty('--tab-i', String(index));
    tablist.dataset.active = tabs[index]?.dataset.tab || '';
  };
  const observer = new MutationObserver(syncTabs);
  tabs.forEach(tab => observer.observe(tab, {attributes: true, attributeFilter: ['aria-selected']}));
  syncTabs();
}
