/* HaxFish Dashboard — Tasks Tab */

async function renderTasksTab(area) {
    try {
        const tasks = await fetchJSON('/api/tasks');
        area.innerHTML = '';
        const grid = document.createElement('div');
        grid.className = 'tasks-grid';

        if (!tasks.length) {
            grid.innerHTML = '<div class="empty-state">No tasks found</div>';
            area.appendChild(grid);
            return;
        }

        for (const t of tasks) {
            const card = document.createElement('div');
            card.className = 'task-card';
            const isRunning = t.status === 'running';
            const statusClass = t.status;

            card.innerHTML = `
                <div class="task-info">
                    <div class="task-name">
                        <span class="status-dot ${statusClass}"></span>
                        ${esc(t.name)}
                        <span class="status-label ${statusClass}">${t.status}</span>
                    </div>
                    <div class="task-meta">
                        <span>Script: ${esc(t.script)}</span>
                        ${t.pid ? `<span>PID: ${t.pid}</span>` : ''}
                        ${isRunning ? `<span>CPU: ${esc(t.cpu)}%</span><span>MEM: ${esc(t.mem)}%</span>` : ''}
                        ${t.enabled_key ? `<span>Setting: ${esc(t.enabled_key)} = ${t.enabled}</span>` : ''}
                    </div>
                    ${t.last_log ? `<div class="task-log">${esc(t.last_log)}</div>` : ''}
                </div>
                <div class="task-actions">
                    ${t.log_file ? `<button class="btn btn-sm btn-log" onclick="viewLog('${esc(t.name)}')">Log</button>` : ''}
                    ${isRunning
                        ? `<button class="btn btn-sm btn-stop" onclick="stopTask('${esc(t.name)}', this)">Stop</button>`
                        : `<button class="btn btn-sm btn-start" onclick="startTask('${esc(t.name)}', this)">Start</button>`
                    }
                </div>
            `;
            grid.appendChild(card);
        }
        area.appendChild(grid);
    } catch (e) {
        area.innerHTML = `<div class="error-state">Failed to load tasks: ${esc(e.message)}</div>`;
    }
}

async function startTask(name, btn) {
    btn.disabled = true;
    btn.textContent = 'Starting...';
    try {
        const res = await fetchJSON(`/api/start/${name}`, { method: 'POST' });
        if (!res.ok) showToast('Error: ' + (res.error || 'unknown'));
        else showToast(res.msg || 'Started');
    } catch (e) {
        showToast('Failed to start: ' + e.message);
    }
    setTimeout(refreshCurrentTab, 1000);
}

async function stopTask(name, btn) {
    if (!confirm(`Stop ${name}?`)) return;
    btn.disabled = true;
    btn.textContent = 'Stopping...';
    try {
        const res = await fetchJSON(`/api/stop/${name}`, { method: 'POST' });
        if (!res.ok) showToast('Error: ' + (res.error || 'unknown'));
        else showToast(res.msg || 'Stopped');
    } catch (e) {
        showToast('Failed to stop: ' + e.message);
    }
    setTimeout(refreshCurrentTab, 1000);
}

async function viewLog(name) {
    let overlay = document.querySelector('.modal-overlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.className = 'modal-overlay';
        overlay.innerHTML = `
            <div class="modal">
                <div class="modal-header">
                    <h3 id="modal-title">Log</h3>
                    <button class="modal-close" onclick="closeLog()">&times;</button>
                </div>
                <div class="modal-body">
                    <pre class="log-content" id="log-content">Loading...</pre>
                </div>
            </div>
        `;
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) closeLog();
        });
        document.body.appendChild(overlay);
    }

    document.getElementById('modal-title').textContent = `${name} — log`;
    document.getElementById('log-content').textContent = 'Loading...';
    overlay.classList.add('active');

    try {
        const res = await fetchJSON(`/api/log/${name}`);
        document.getElementById('log-content').textContent = res.log || 'Empty';
        const body = document.querySelector('.modal-body');
        body.scrollTop = body.scrollHeight;
    } catch (e) {
        document.getElementById('log-content').textContent = 'Error: ' + e.message;
    }
}

function closeLog() {
    const overlay = document.querySelector('.modal-overlay');
    if (overlay) overlay.classList.remove('active');
}

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeLog();
});
