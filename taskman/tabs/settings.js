/* HaxFish Dashboard — Settings Tab */

const SETTINGS_GROUPS = {
    'Copy Trading': ['copy_enabled', 'copy_max_shares', 'copy_max_positions', 'copy_tp_pct',
                      'copy_poll_interval', 'copy_max_price_slip',
                      'copy_max_daily_loss', 'copy_max_trades_per_day',
                      'copy_max_stake_per_wallet', 'copy_max_stake_per_slug',
                      'copy_max_total_exposure_usd'],
    'Cross Arb (Kalshi)': ['xarb_enabled', 'xarb_poll_interval', 'xarb_min_edge'],
    'Cross Arb (Betfair)': ['cross_arb_strict_enabled', 'cross_arb_strict_stake',
                             'cross_arb_loose_enabled', 'cross_arb_loose_stake',
                             'cross_arb_live_trading', 'cross_arb_auto_tp_sell',
                             'cross_arb_take_profit', 'cross_arb_stop_loss',
                             'cross_arb_order_ttl', 'cross_scan_interval'],
    'Polymarket': ['poly_scan_enabled', 'polymarket_live_trading', 'polymarket_auto_tp_sell',
                    'market_take_profit', 'poly_scan_interval', 'poly_bet_tiers',
                    'minPrice', 'maxPrice', 'divergenceThreshold', 'positionSize'],
    'Betfair': ['betfairEnabled', 'betfair_live_trading', 'betfair_auto_tp_sell',
                 'bf_stake', 'bf_max_days'],
    'NBA / Sports': ['nba_enabled', 'nba_live_trading', 'nba_auto_tp_sell',
                      'nba_stake', 'nba_min_confidence', 'nba_divergence',
                      'nba_min_liquidity', 'sports_min_liquidity'],
    'General': ['autoTrade', 'sportsOnly', 'maxAgents', 'maxDailySpend', 'maxExposure',
                 'defaultTakeProfit', 'defaultStopLoss'],
    'Schedule': ['scan_start_hour', 'scan_end_hour', 'monitor_interval', 'dashboard_refresh'],
};

const COPY_PRESETS = {
    testing: {
        label: 'Testing',
        desc: '~$2.50/trade, tight caps',
        values: {
            copy_max_shares: 5,
            copy_max_positions: 20,
            copy_max_trades_per_day: 10,
            copy_max_daily_loss: 20,
            copy_max_stake_per_wallet: 15,
            copy_max_stake_per_slug: 10,
            copy_max_total_exposure_usd: 50,
        },
    },
    conservative: {
        label: 'Conservative',
        desc: '~$2.50/trade, wider caps',
        values: {
            copy_max_shares: 5,
            copy_max_positions: 50,
            copy_max_trades_per_day: 30,
            copy_max_daily_loss: 50,
            copy_max_stake_per_wallet: 30,
            copy_max_stake_per_slug: 15,
            copy_max_total_exposure_usd: 100,
        },
    },
    aggressive: {
        label: 'Aggressive',
        desc: '100 trades/day, $100 loss cap',
        values: {
            copy_max_shares: 5,
            copy_max_positions: 100,
            copy_max_trades_per_day: 100,
            copy_max_daily_loss: 100,
            copy_max_stake_per_wallet: 50,
            copy_max_stake_per_slug: 25,
            copy_max_total_exposure_usd: 250,
        },
    },
};

const SETTING_DESCRIPTIONS = {
    copy_enabled: 'Enable/disable the copy scanner',
    copy_max_shares: 'Max shares per trade (min order size on Polymarket is usually 5)',
    copy_max_positions: 'Max open trades at any time',
    copy_tp_pct: 'Take-profit % (null = no auto TP)',
    copy_poll_interval: 'Seconds between each scan cycle',
    copy_max_price_slip: 'Max price slippage allowed vs source entry price',
    copy_max_daily_loss: 'Stop trading if realised losses today exceed this USD amount',
    copy_max_trades_per_day: 'Max new trades per UTC day',
    copy_max_stake_per_wallet: 'Max total open stake from any single source wallet (USD)',
    copy_max_stake_per_slug: 'Max total open stake on any single market/slug (USD)',
    copy_max_total_exposure_usd: 'Global cap on total open stake across ALL trades (USD)',
};

let originalSettings = {};

async function renderSettingsTab(area) {
    try {
        const settings = await fetchJSON('/api/settings');
        originalSettings = JSON.parse(JSON.stringify(settings));
        area.innerHTML = '';

        const form = document.createElement('div');
        form.className = 'settings-form';

        // Track which keys are grouped
        const grouped = new Set();

        for (const [groupName, keys] of Object.entries(SETTINGS_GROUPS)) {
            const section = document.createElement('div');
            section.className = 'settings-group';

            const header = document.createElement('div');
            header.className = 'settings-group-header';
            header.textContent = groupName;
            header.onclick = () => {
                section.classList.toggle('collapsed');
            };
            section.appendChild(header);

            const body = document.createElement('div');
            body.className = 'settings-group-body';

            // Add preset buttons for Copy Trading group
            if (groupName === 'Copy Trading') {
                const presetBar = document.createElement('div');
                presetBar.className = 'filter-bar';
                presetBar.style.padding = '12px 0 8px';

                const presetLabel = document.createElement('span');
                presetLabel.className = 'setting-label';
                presetLabel.style.flex = '0';
                presetLabel.style.whiteSpace = 'nowrap';
                presetLabel.style.marginRight = '8px';
                presetLabel.textContent = 'Presets:';
                presetBar.appendChild(presetLabel);

                for (const [id, preset] of Object.entries(COPY_PRESETS)) {
                    const btn = document.createElement('button');
                    btn.className = 'filter-btn';
                    btn.textContent = preset.label;
                    btn.title = preset.desc;
                    btn.onclick = () => applyPreset(preset.values);
                    presetBar.appendChild(btn);
                }
                body.appendChild(presetBar);
            }

            for (const key of keys) {
                if (!(key in settings)) continue;
                grouped.add(key);
                body.appendChild(renderSettingField(key, settings[key]));
            }

            section.appendChild(body);
            form.appendChild(section);
        }

        // Ungrouped settings
        const ungroupedKeys = Object.keys(settings).filter(k => !grouped.has(k));
        if (ungroupedKeys.length) {
            const section = document.createElement('div');
            section.className = 'settings-group';

            const header = document.createElement('div');
            header.className = 'settings-group-header';
            header.textContent = 'Other';
            header.onclick = () => section.classList.toggle('collapsed');
            section.appendChild(header);

            const body = document.createElement('div');
            body.className = 'settings-group-body';
            for (const key of ungroupedKeys) {
                body.appendChild(renderSettingField(key, settings[key]));
            }
            section.appendChild(body);
            form.appendChild(section);
        }

        // Save button
        const actions = document.createElement('div');
        actions.className = 'settings-actions';
        const saveBtn = document.createElement('button');
        saveBtn.className = 'btn btn-start';
        saveBtn.textContent = 'Save Settings';
        saveBtn.onclick = () => saveSettings(form);
        actions.appendChild(saveBtn);
        form.appendChild(actions);

        area.appendChild(form);
    } catch (e) {
        area.innerHTML = `<div class="error-state">Failed to load settings: ${esc(e.message)}</div>`;
    }
}

function renderSettingField(key, value) {
    const row = document.createElement('div');
    row.className = 'setting-row';

    const label = document.createElement('label');
    label.className = 'setting-label';
    label.textContent = key;
    if (SETTING_DESCRIPTIONS[key]) {
        const hint = document.createElement('span');
        hint.className = 'setting-hint';
        hint.textContent = SETTING_DESCRIPTIONS[key];
        label.appendChild(hint);
    }
    row.appendChild(label);

    const inputWrap = document.createElement('div');
    inputWrap.className = 'setting-input';

    if (typeof value === 'boolean') {
        const toggle = document.createElement('button');
        toggle.className = 'toggle-switch' + (value ? ' on' : '');
        toggle.dataset.key = key;
        toggle.dataset.type = 'boolean';
        toggle.dataset.value = value;
        toggle.textContent = value ? 'ON' : 'OFF';
        toggle.onclick = () => {
            const newVal = toggle.dataset.value !== 'true';
            toggle.dataset.value = newVal;
            toggle.textContent = newVal ? 'ON' : 'OFF';
            toggle.className = 'toggle-switch' + (newVal ? ' on' : '');
        };
        inputWrap.appendChild(toggle);
    } else if (typeof value === 'object' && value !== null) {
        const textarea = document.createElement('textarea');
        textarea.className = 'setting-json';
        textarea.dataset.key = key;
        textarea.dataset.type = 'json';
        textarea.value = JSON.stringify(value, null, 2);
        textarea.rows = Math.min(10, JSON.stringify(value, null, 2).split('\n').length + 1);
        inputWrap.appendChild(textarea);
    } else if (typeof value === 'number') {
        const input = document.createElement('input');
        input.type = 'number';
        input.step = Number.isInteger(value) ? '1' : '0.01';
        input.className = 'setting-number';
        input.dataset.key = key;
        input.dataset.type = 'number';
        input.value = value;
        inputWrap.appendChild(input);
    } else if (value === null) {
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'setting-text';
        input.dataset.key = key;
        input.dataset.type = 'nullable';
        input.value = '';
        input.placeholder = 'null';
        inputWrap.appendChild(input);
    } else {
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'setting-text';
        input.dataset.key = key;
        input.dataset.type = 'string';
        input.value = value;
        inputWrap.appendChild(input);
    }

    row.appendChild(inputWrap);
    return row;
}

function applyPreset(values) {
    for (const [key, val] of Object.entries(values)) {
        const el = document.querySelector(`[data-key="${key}"]`);
        if (!el) continue;
        if (el.dataset.type === 'number') {
            el.value = val;
            el.style.borderColor = 'var(--warning)';
        } else if (el.dataset.type === 'boolean') {
            el.dataset.value = val;
            el.textContent = val ? 'ON' : 'OFF';
            el.className = 'toggle-switch' + (val ? ' on' : '');
        }
    }
    showToast('Preset applied — review and Save');
}

async function saveSettings(form) {
    const updates = {};

    // Collect all inputs
    form.querySelectorAll('[data-key]').forEach(el => {
        const key = el.dataset.key;
        const type = el.dataset.type;
        let val;

        if (type === 'boolean') {
            val = el.dataset.value === 'true';
        } else if (type === 'number') {
            val = el.value === '' ? 0 : parseFloat(el.value);
        } else if (type === 'json') {
            try {
                val = JSON.parse(el.value);
            } catch (e) {
                showToast(`Invalid JSON for ${key}: ${e.message}`);
                el.style.borderColor = 'var(--loss)';
                return;
            }
        } else if (type === 'nullable') {
            val = el.value === '' ? null : el.value;
            // Try to parse as number if it looks like one
            if (val !== null && !isNaN(val)) val = parseFloat(val);
        } else {
            val = el.value;
        }

        // Only include changed values
        if (JSON.stringify(val) !== JSON.stringify(originalSettings[key])) {
            updates[key] = val;
        }
    });

    if (Object.keys(updates).length === 0) {
        showToast('No changes to save');
        return;
    }

    try {
        const res = await fetchJSON('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(updates),
        });
        if (res.ok) {
            originalSettings = res.settings;
            showToast(`Saved ${Object.keys(updates).length} setting(s)`);
        } else {
            showToast('Error: ' + (res.error || 'unknown'));
        }
    } catch (e) {
        showToast('Save failed: ' + e.message);
    }
}
