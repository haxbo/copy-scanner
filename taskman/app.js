/* HaxFish Dashboard — Core */

let currentTab = 'tasks';
let refreshTimer = null;

async function fetchJSON(url, opts = {}) {
    const res = await fetch(url, opts);
    return res.json();
}

function esc(s) {
    if (s == null) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function showToast(msg, duration = 3000) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.classList.add('visible');
    setTimeout(() => t.classList.remove('visible'), duration);
}

// ── Tab switching ──────────────────────────────────────────────

function switchTab(tab) {
    currentTab = tab;
    localStorage.setItem('haxfish_tab', tab);

    document.querySelectorAll('.tab').forEach(el => {
        el.classList.toggle('active', el.dataset.tab === tab);
    });

    const area = document.getElementById('content-area');
    area.innerHTML = '<div class="loading">Loading...</div>';

    if (tab === 'tasks') {
        renderTasksTab(area);
    } else if (tab === 'settings') {
        renderSettingsTab(area);
    } else {
        renderDataTab(area, tab);
    }

    updateRefreshTime();
}

function refreshCurrentTab() {
    switchTab(currentTab);
}

function updateRefreshTime() {
    document.getElementById('last-refresh').textContent =
        new Date().toLocaleTimeString();
}

// ── Init ──────────────────────────────────────────────────────

document.getElementById('tab-bar').addEventListener('click', (e) => {
    if (e.target.classList.contains('tab')) {
        switchTab(e.target.dataset.tab);
    }
});

// Restore last tab or default to tasks
const saved = localStorage.getItem('haxfish_tab');
switchTab(saved || 'tasks');

// Auto-refresh tasks tab only, every 10s
refreshTimer = setInterval(() => {
    if (currentTab === 'tasks') refreshCurrentTab();
}, 10000);
