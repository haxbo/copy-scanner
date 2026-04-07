/* HaxFish Dashboard — Data Table with summary rows + expandable details */

const TABLE_CONFIGS = {
    copy_wallets: {
        db: 'copy_scanner', table: 'copy_wallets', perPage: 50,
        filters: [], defaultSort: 'id',
        // Wallets are simple — show all columns, no summary/detail split
        summary: null,
    },
    copy_positions: {
        db: 'copy_scanner', table: 'copy_positions', perPage: 50,
        filters: ['all', 'open', 'closed'], defaultSort: 'last_seen_at',
        summary: [
            { col: 'title',       label: 'Event',   flex: 3 },
            { col: 'outcome',     label: 'Side',    flex: 1 },
            { col: 'cur_price',   label: 'Price',   flex: 1, fmt: 'price' },
            { col: 'percent_pnl', label: 'P&L %',   flex: 1, fmt: 'pnl_pct', pnl: true },
            { col: 'cash_pnl',    label: 'P&L $',   flex: 1, fmt: 'money', pnl: true },
            { col: 'last_seen_at',label: 'Updated',  flex: 1, fmt: 'date' },
        ],
        link: row => row.slug ? `https://polymarket.com/event/${row.slug}` : null,
        hidden: ['id', 'icon', 'wallet_id'],
    },
    copy_trades: {
        db: 'copy_scanner', table: 'copy_trades', perPage: 50,
        filters: ['all', 'open', 'closed'], defaultSort: 'placed_at',
        summary: [
            { col: 'title',          label: 'Event',   flex: 3 },
            { col: 'outcome',        label: 'Side',    flex: 1 },
            { col: 'our_entry_price',label: 'Entry',   flex: 1, fmt: 'price' },
            { col: 'cur_price',      label: 'Now',     flex: 1, fmt: 'price' },
            { col: 'pnl_usd',        label: 'P&L $',   flex: 1, fmt: 'money', pnl: true },
            { col: 'status',         label: 'Status',  flex: 1, fmt: 'status' },
            { col: 'placed_at',      label: 'Placed',  flex: 1, fmt: 'date' },
        ],
        link: row => row.slug ? `https://polymarket.com/event/${row.slug}` : null,
        hidden: ['id'],
    },
    copy_skips: {
        db: 'copy_scanner', table: 'copy_skips', perPage: 50,
        filters: [], defaultSort: 'id',
        summary: [
            { col: 'title',            label: 'Event',  flex: 3 },
            { col: 'outcome',          label: 'Side',   flex: 1 },
            { col: 'reason',           label: 'Reason', flex: 2 },
            { col: 'source_pseudonym', label: 'Source',  flex: 1 },
            { col: 'skipped_at',       label: 'When',   flex: 1, fmt: 'date' },
        ],
        hidden: ['id'],
    },
    xarb_scans: {
        db: 'cross_platform_arb', table: 'xarb_scans', perPage: 50,
        filters: [], defaultSort: 'id',
        summary: [
            { col: 'game',       label: 'Game',    flex: 3 },
            { col: 'sport',      label: 'Sport',   flex: 1 },
            { col: 'edge',       label: 'Edge',    flex: 1, fmt: 'pct', pnl: true },
            { col: 'strategy',   label: 'Strategy',flex: 1 },
            { col: 'scanned_at', label: 'When',    flex: 1, fmt: 'date' },
        ],
        hidden: ['id'],
    },
    xarb_alerts: {
        db: 'cross_platform_arb', table: 'xarb_alerts', perPage: 50,
        filters: [], defaultSort: 'id',
        summary: [
            { col: 'game',       label: 'Game',       flex: 3 },
            { col: 'sport',      label: 'Sport',      flex: 1 },
            { col: 'edge',       label: 'Edge',       flex: 1, fmt: 'pct', pnl: true },
            { col: 'est_profit', label: 'Est Profit', flex: 1, fmt: 'money', pnl: true },
            { col: 'strategy',   label: 'Strategy',   flex: 1 },
            { col: 'alerted_at', label: 'When',       flex: 1, fmt: 'date' },
        ],
        hidden: ['id'],
    },
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
            expandedId: null,
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
        if (cfg.summary) {
            renderSummaryTable(area, tab, cfg, state, data);
        } else {
            renderFlatTable(area, tab, cfg, state, data);
        }
    } catch (e) {
        area.innerHTML = `<div class="error-state">Failed to load: ${esc(e.message)}</div>`;
    }
}


// ── Helper: convert row array + columns array to object ──

function rowToObj(columns, row) {
    const obj = {};
    for (let i = 0; i < columns.length; i++) obj[columns[i]] = row[i];
    return obj;
}


// ── Summary table (positions, trades, skips, xarb) ──

function renderSummaryTable(area, tab, cfg, state, data) {
    area.innerHTML = '';

    // Filter bar
    renderFilterBar(area, tab, cfg, state, data);

    if (!data.rows.length) {
        area.innerHTML += '<div class="empty-state">No data</div>';
        return;
    }

    const list = document.createElement('div');
    list.className = 'summary-list';

    // Header row
    const headerEl = document.createElement('div');
    headerEl.className = 'summary-row summary-header';
    for (const col of cfg.summary) {
        const cell = document.createElement('div');
        cell.className = 'summary-cell';
        cell.style.flex = col.flex;
        cell.textContent = col.label;

        // Sortable
        const isSorted = state.sort === col.col;
        if (isSorted) {
            cell.classList.add('sorted');
            cell.textContent += state.order === 'asc' ? ' \u25B2' : ' \u25BC';
        }
        cell.style.cursor = 'pointer';
        cell.onclick = () => {
            if (state.sort === col.col) {
                state.order = state.order === 'asc' ? 'desc' : 'asc';
            } else {
                state.sort = col.col;
                state.order = 'desc';
            }
            state.page = 1;
            renderDataTab(area, tab);
        };

        headerEl.appendChild(cell);
    }
    list.appendChild(headerEl);

    // Data rows
    for (const row of data.rows) {
        const obj = rowToObj(data.columns, row);
        const idIdx = data.columns.indexOf('id');
        const rowId = idIdx >= 0 ? row[idIdx] : null;
        const isExpanded = state.expandedId === rowId;

        // Summary row
        const rowEl = document.createElement('div');
        rowEl.className = 'summary-row' + (isExpanded ? ' expanded' : '');
        rowEl.onclick = () => {
            state.expandedId = isExpanded ? null : rowId;
            renderDataTab(area, tab);
        };

        for (const col of cfg.summary) {
            const cell = document.createElement('div');
            cell.className = 'summary-cell';
            cell.style.flex = col.flex;

            const val = obj[col.col];
            cell.textContent = fmtValue(val, col.fmt);

            // P&L coloring
            if (col.pnl && val != null) {
                const num = parseFloat(val);
                if (num > 0) cell.style.color = 'var(--profit)';
                else if (num < 0) cell.style.color = 'var(--loss)';
            }

            // Status coloring
            if (col.fmt === 'status') {
                if (val === 'open') cell.style.color = 'var(--profit)';
                else if (val === 'closed') cell.style.color = 'var(--text-3)';
                else if (val === 'pending') cell.style.color = 'var(--warning)';
            }

            rowEl.appendChild(cell);
        }

        list.appendChild(rowEl);

        // Detail panel (expanded)
        if (isExpanded) {
            const detail = document.createElement('div');
            detail.className = 'detail-panel';

            // Link to Polymarket
            if (cfg.link) {
                const url = cfg.link(obj);
                if (url) {
                    const linkEl = document.createElement('a');
                    linkEl.href = url;
                    linkEl.target = '_blank';
                    linkEl.className = 'detail-link';
                    linkEl.textContent = 'View on Polymarket \u2197';
                    detail.appendChild(linkEl);
                }
            }

            // All fields
            const grid = document.createElement('div');
            grid.className = 'detail-grid';
            for (const col of data.columns) {
                if (cfg.hidden && cfg.hidden.includes(col)) continue;
                const label = document.createElement('span');
                label.className = 'detail-label';
                label.textContent = col;
                const value = document.createElement('span');
                value.className = 'detail-value';
                const v = obj[col];
                value.textContent = v == null ? '' : String(v);

                // Enabled toggle in detail
                if (col === 'enabled') {
                    value.textContent = v ? 'Yes' : 'No';
                    value.style.color = v ? 'var(--profit)' : 'var(--text-3)';
                    value.style.cursor = 'pointer';
                    value.onclick = async (e) => {
                        e.stopPropagation();
                        const res = await fetchJSON(
                            `/api/db/toggle/${cfg.db}/${cfg.table}/${rowId}/enabled`,
                            { method: 'POST' });
                        if (res.ok) {
                            showToast(`enabled → ${res.value ? 'Yes' : 'No'}`);
                            renderDataTab(area, tab);
                        }
                    };
                }

                grid.appendChild(label);
                grid.appendChild(value);
            }
            detail.appendChild(grid);
            list.appendChild(detail);
        }
    }

    area.appendChild(list);
    renderPagination(area, tab, state, data);
}


// ── Flat table (wallets — simple, few columns) ──

function renderFlatTable(area, tab, cfg, state, data) {
    area.innerHTML = '';

    renderFilterBar(area, tab, cfg, state, data);

    if (!data.rows.length) {
        area.innerHTML += '<div class="empty-state">No data</div>';
        return;
    }

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

            td.textContent = formatCell(col, val);

            if (col === 'enabled') {
                td.style.color = val ? 'var(--profit)' : 'var(--text-3)';
                td.textContent = val ? 'Yes' : 'No';
                td.style.cursor = 'pointer';
                const idIdx = data.columns.indexOf('id');
                if (idIdx >= 0) {
                    const rowId = row[idIdx];
                    td.onclick = async () => {
                        const res = await fetchJSON(
                            `/api/db/toggle/${cfg.db}/${cfg.table}/${rowId}/enabled`,
                            { method: 'POST' });
                        if (res.ok) {
                            showToast(`enabled → ${res.value ? 'Yes' : 'No'}`);
                            renderDataTab(area, tab);
                        } else {
                            showToast('Error: ' + (res.error || 'unknown'));
                        }
                    };
                }
            }

            tr.appendChild(td);
        }
        tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    wrapper.appendChild(table);
    area.appendChild(wrapper);

    renderPagination(area, tab, state, data);
}


// ── Shared: filter bar ──

function renderFilterBar(area, tab, cfg, state, data) {
    const filterBar = document.createElement('div');
    filterBar.className = 'filter-bar';

    if (cfg.filters.length) {
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
    }

    const count = document.createElement('span');
    count.className = 'filter-count';
    count.textContent = `${data.total} rows`;
    filterBar.appendChild(count);
    area.appendChild(filterBar);
}


// ── Shared: pagination ──

function renderPagination(area, tab, state, data) {
    if (data.pages <= 1) return;

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


// ── Formatters ──

function fmtValue(val, fmt) {
    if (val == null) return '';
    if (fmt === 'price') return parseFloat(val).toFixed(3);
    if (fmt === 'money') {
        const n = parseFloat(val);
        return (n < 0 ? '-$' : '$') + Math.abs(n).toFixed(2);
    }
    if (fmt === 'pnl_pct') return parseFloat(val).toFixed(1) + '%';
    if (fmt === 'pct') return (parseFloat(val) * 100).toFixed(1) + '%';
    if (fmt === 'date') {
        const s = String(val);
        // Show just date + time, drop seconds
        if (s.length >= 16) return s.substring(0, 16).replace('T', ' ');
        return s;
    }
    if (fmt === 'status') {
        return String(val).charAt(0).toUpperCase() + String(val).slice(1);
    }
    // Default: truncate long strings
    const s = String(val);
    return s.length > 50 ? s.substring(0, 47) + '...' : s;
}

function formatCell(col, val) {
    if (val == null) return '';

    if (typeof val === 'string' && val.length > 60) {
        return val.substring(0, 57) + '...';
    }

    if (typeof val === 'number') {
        if (col.includes('price') || col.includes('pnl_pct') || col === 'edge') {
            return val.toFixed(4);
        }
        if (col.includes('pnl_usd') || col.includes('stake') || col.includes('value')
            || col === 'est_profit') {
            return '$' + val.toFixed(2);
        }
    }

    return String(val);
}
