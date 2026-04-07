/* HaxFish Dashboard — Generic Data Table Tab */

const TABLE_CONFIGS = {
    copy_wallets:    { db: 'copy_scanner',       table: 'copy_wallets',    perPage: 50,  filters: [], defaultSort: 'id', pnl: [] },
    copy_positions:  { db: 'copy_scanner',       table: 'copy_positions',  perPage: 50,  filters: ['all','open','closed'], defaultSort: 'id', pnl: ['cash_pnl', 'percent_pnl'] },
    copy_trades:     { db: 'copy_scanner',       table: 'copy_trades',     perPage: 50,  filters: ['all','open','closed'], defaultSort: 'id', pnl: ['pnl_pct', 'pnl_usd'] },
    copy_skips:      { db: 'copy_scanner',       table: 'copy_skips',      perPage: 50,  filters: [], defaultSort: 'id', pnl: [] },
    xarb_scans:      { db: 'cross_platform_arb', table: 'xarb_scans',     perPage: 50,  filters: [], defaultSort: 'id', pnl: ['edge'] },
    xarb_alerts:     { db: 'cross_platform_arb', table: 'xarb_alerts',    perPage: 50,  filters: [], defaultSort: 'id', pnl: ['edge', 'est_profit'] },
};

// Per-tab state
const tabState = {};

function getState(tab) {
    if (!tabState[tab]) {
        const cfg = TABLE_CONFIGS[tab];
        tabState[tab] = {
            page: 1,
            perPage: cfg.perPage,
            sort: cfg.defaultSort,
            order: 'desc',
            preset: cfg.filters.length ? cfg.filters[1] || cfg.filters[0] : null,
        };
    }
    return tabState[tab];
}

async function renderDataTab(area, tab) {
    const cfg = TABLE_CONFIGS[tab];
    if (!cfg) {
        area.innerHTML = `<div class="error-state">Unknown tab: ${tab}</div>`;
        return;
    }

    const state = getState(tab);
    const params = new URLSearchParams({
        page: state.page,
        per_page: state.perPage,
        sort: state.sort,
        order: state.order,
    });
    if (state.preset && state.preset !== 'all') {
        params.set('preset', state.preset);
    }

    try {
        const data = await fetchJSON(`/api/db/${cfg.db}/${cfg.table}?${params}`);
        if (data.error) {
            area.innerHTML = `<div class="error-state">${esc(data.error)}</div>`;
            return;
        }
        renderTable(area, tab, cfg, state, data);
    } catch (e) {
        area.innerHTML = `<div class="error-state">Failed to load: ${esc(e.message)}</div>`;
    }
}

function renderTable(area, tab, cfg, state, data) {
    area.innerHTML = '';

    // ── Filter bar ──
    if (cfg.filters.length) {
        const filterBar = document.createElement('div');
        filterBar.className = 'filter-bar';
        for (const f of cfg.filters) {
            const btn = document.createElement('button');
            btn.className = 'filter-btn' + ((state.preset || 'all') === f ? ' active' : '');
            btn.textContent = f.charAt(0).toUpperCase() + f.slice(1);
            btn.onclick = () => {
                state.preset = f;
                state.page = 1;
                renderDataTab(area, tab);
            };
            filterBar.appendChild(btn);
        }
        // Row count
        const count = document.createElement('span');
        count.className = 'filter-count';
        count.textContent = `${data.total} rows`;
        filterBar.appendChild(count);
        area.appendChild(filterBar);
    } else {
        const countBar = document.createElement('div');
        countBar.className = 'filter-bar';
        const count = document.createElement('span');
        count.className = 'filter-count';
        count.textContent = `${data.total} rows`;
        countBar.appendChild(count);
        area.appendChild(countBar);
    }

    if (!data.rows.length) {
        area.innerHTML += '<div class="empty-state">No data</div>';
        return;
    }

    // ── Table ──
    const wrapper = document.createElement('div');
    wrapper.className = 'table-wrapper';
    const table = document.createElement('table');
    table.className = 'data-table';

    // Header
    const thead = document.createElement('thead');
    const headerRow = document.createElement('tr');
    for (const col of data.columns) {
        const th = document.createElement('th');
        th.textContent = col;
        const isSorted = state.sort === col;
        if (isSorted) {
            th.classList.add('sorted');
            th.textContent += state.order === 'asc' ? ' \u25B2' : ' \u25BC';
        }
        th.onclick = () => {
            if (state.sort === col) {
                state.order = state.order === 'asc' ? 'desc' : 'asc';
            } else {
                state.sort = col;
                state.order = 'desc';
            }
            state.page = 1;
            renderDataTab(area, tab);
        };
        headerRow.appendChild(th);
    }
    thead.appendChild(headerRow);
    table.appendChild(thead);

    // Body
    const tbody = document.createElement('tbody');
    for (const row of data.rows) {
        const tr = document.createElement('tr');
        for (let i = 0; i < data.columns.length; i++) {
            const td = document.createElement('td');
            const col = data.columns[i];
            const val = row[i];

            // Format cell
            td.textContent = formatCell(col, val);

            // P&L coloring
            if (cfg.pnl.includes(col) && val != null) {
                const num = parseFloat(val);
                if (num > 0) td.style.color = 'var(--profit)';
                else if (num < 0) td.style.color = 'var(--loss)';
            }

            // Status coloring
            if (col === 'status') {
                if (val === 'open' || val === 'running') td.style.color = 'var(--profit)';
                else if (val === 'closed') td.style.color = 'var(--text-3)';
                else if (val === 'pending') td.style.color = 'var(--warning)';
            }

            // Enabled coloring
            if (col === 'enabled') {
                td.style.color = val ? 'var(--profit)' : 'var(--text-3)';
                td.textContent = val ? 'Yes' : 'No';
            }

            tr.appendChild(td);
        }
        tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    wrapper.appendChild(table);
    area.appendChild(wrapper);

    // ── Pagination ──
    if (data.pages > 1) {
        const pag = document.createElement('div');
        pag.className = 'pagination';

        const prev = document.createElement('button');
        prev.className = 'btn btn-sm';
        prev.textContent = 'Prev';
        prev.disabled = state.page <= 1;
        prev.onclick = () => { state.page--; renderDataTab(area, tab); };

        const info = document.createElement('span');
        info.className = 'page-info';
        info.textContent = `Page ${data.page} of ${data.pages}`;

        const next = document.createElement('button');
        next.className = 'btn btn-sm';
        next.textContent = 'Next';
        next.disabled = state.page >= data.pages;
        next.onclick = () => { state.page++; renderDataTab(area, tab); };

        const perPageSel = document.createElement('select');
        perPageSel.className = 'per-page-select';
        for (const n of [25, 50, 100]) {
            const opt = document.createElement('option');
            opt.value = n;
            opt.textContent = `${n} / page`;
            opt.selected = state.perPage === n;
            perPageSel.appendChild(opt);
        }
        perPageSel.onchange = () => {
            state.perPage = parseInt(perPageSel.value);
            state.page = 1;
            renderDataTab(area, tab);
        };

        pag.appendChild(prev);
        pag.appendChild(info);
        pag.appendChild(next);
        pag.appendChild(perPageSel);
        area.appendChild(pag);
    }
}

function formatCell(col, val) {
    if (val == null) return '';

    // Truncate long strings (asset IDs, slugs)
    if (typeof val === 'string' && val.length > 60) {
        return val.substring(0, 57) + '...';
    }

    // Format floats
    if (typeof val === 'number') {
        if (col.includes('price') || col.includes('pnl_pct') || col === 'edge'
            || col === 'divergence' || col === 'cross_sum') {
            return val.toFixed(4);
        }
        if (col.includes('pnl_usd') || col.includes('stake') || col.includes('value')
            || col === 'est_profit') {
            return '$' + val.toFixed(2);
        }
    }

    return String(val);
}
