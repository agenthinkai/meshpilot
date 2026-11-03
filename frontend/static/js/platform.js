/**
 * MeshPilot Dashboard — Fit Report and Privacy & Audit pages.
 * Loaded after app.js and uses its apiFetch and state. Everything from the
 * server goes through esc() because reports contain customer text.
 */

function esc(value) {
  const div = document.createElement('div');
  div.textContent = value === null || value === undefined ? '' : String(value);
  return div.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function pct(value) {
  return value === null || value === undefined ? '—' : `${Math.round(value * 100)}%`;
}

function usd(value) {
  return value === null || value === undefined ? '—' : '$' + Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// ── Fit Report ───────────────────────────────────────────────────────────────
let fitPollTimer = null;
let currentFitReport = null;

async function loadFit() {
  await loadFitRuns();
  if (!document.getElementById('fitSuite').value) await loadFitSample();
}

async function loadFitSample() {
  const sample = await apiFetch('/api/v1/fit/sample');
  document.getElementById('fitSuite').value = JSON.stringify(sample, null, 2);
}

function loadFitFile(input) {
  const file = input.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => { document.getElementById('fitSuite').value = reader.result; };
  reader.readAsText(file);
}

async function startFitRun() {
  const message = document.getElementById('fitMessage');
  message.textContent = '';
  let suite;
  try {
    suite = JSON.parse(document.getElementById('fitSuite').value);
  } catch (e) {
    message.textContent = `That isn't valid JSON: ${e.message}`;
    return;
  }
  try {
    const run = await apiFetch('/api/v1/fit/runs', 'POST', suite);
    await loadFitRuns();
    watchFitRun(run.id);
  } catch (e) {
    message.textContent = typeof e.message === 'string' ? e.message : 'The test suite was rejected. Check its format.';
  }
}

function watchFitRun(id) {
  clearInterval(fitPollTimer);
  fitPollTimer = setInterval(async () => {
    if (state.currentPage !== 'fit') { clearInterval(fitPollTimer); return; }
    const run = await apiFetch(`/api/v1/fit/runs/${id}`).catch(() => null);
    await loadFitRuns();
    if (run && run.status !== 'pending' && run.status !== 'running') {
      clearInterval(fitPollTimer);
      if (run.status === 'completed') showFitReport(id);
    }
  }, 3000);
}

async function loadFitRuns() {
  const runs = await apiFetch('/api/v1/fit/runs');
  const body = document.getElementById('fitRunsBody');
  if (!runs.length) {
    body.innerHTML = '<tr><td colspan="4" class="empty-cell">No fit reports yet</td></tr>';
    return;
  }
  body.innerHTML = runs.map(run => {
    const active = run.status === 'pending' || run.status === 'running';
    const progress = active && run.progress ? ` ${run.progress.done}/${run.progress.total}` : '';
    const badge = run.status === 'completed' ? 'success' : run.status === 'failed' ? 'error' : 'pending';
    return `<tr>
      <td>${esc(run.name)}</td>
      <td><span class="badge badge-${badge}">${esc(run.status)}${progress}</span>${run.error ? `<div class="cell-note">${esc(run.error)}</div>` : ''}</td>
      <td>${new Date(run.created_at).toLocaleString()}</td>
      <td class="row-actions">
        ${run.status === 'completed' ? `<button class="btn-sm" onclick="showFitReport('${esc(run.id)}')">View</button>` : ''}
        ${active ? '' : `<button class="btn-sm" onclick="deleteFitRun('${esc(run.id)}')">Delete</button>`}
      </td>
    </tr>`;
  }).join('');
}

async function deleteFitRun(id) {
  if (!confirm('Delete this report and its test documents?')) return;
  await apiFetch(`/api/v1/fit/runs/${id}`, 'DELETE');
  if (currentFitReport && currentFitReport.id === id) {
    currentFitReport = null;
    document.getElementById('fitReport').innerHTML = '';
  }
  await loadFitRuns();
}

function downloadFitReport() {
  if (!currentFitReport) return;
  const blob = new Blob([JSON.stringify(currentFitReport.report, null, 2)], { type: 'application/json' });
  downloadBlob(blob, 'meshpilot-fit-report.json');
}

async function showFitReport(id) {
  currentFitReport = await apiFetch(`/api/v1/fit/runs/${id}`);
  const report = currentFitReport.report;
  const tasks = Object.keys(report.recommendations);

  const recommendations = tasks.map(task => {
    const pick = report.recommendations[task];
    if (!pick) return '';
    return `<tr>
      <td>${esc(task)}</td>
      <td>${esc(pick.name)}</td>
      <td>${pct(pick.accuracy)}</td>
      <td>${pick.mean_latency_s ?? '—'} s</td>
      <td>${pick.meets_threshold
        ? '<span class="badge badge-success">Fits</span>'
        : '<span class="badge badge-error">No model met the bar</span>'}</td>
    </tr>`;
  }).join('');

  const matrixHead = report.models.map(m => `<th>${esc(m.name)}</th>`).join('');
  const matrix = tasks.map(task => `<tr><td>${esc(task)}</td>${report.models.map(m => {
    const t = m.tasks[task];
    return `<td>${t ? `${pct(t.accuracy)} <span class="cell-note">${t.passed}/${t.cases} fully right</span>` : '—'}</td>`;
  }).join('')}</tr>`).join('');

  const performance = report.models.map(m => {
    const e = m.economics;
    return `<tr>
      <td>${esc(m.name)}</td>
      <td>${m.generation_tps ?? '—'} tok/s</td>
      <td>${m.latency_p50_s ?? '—'} s / ${m.latency_p95_s ?? '—'} s</td>
      <td>${m.memory_mb ? Math.round(m.memory_mb) + ' MB' : '—'}</td>
      <td>${e ? e.requests_per_server_per_day.toLocaleString() : '—'}</td>
      <td>${e ? e.servers_needed : '—'}</td>
      <td>${e ? usd(e.monthly_server_usd) : '—'}</td>
    </tr>`;
  }).join('');

  const workload = report.workload;
  const costNote = workload
    ? `For ${workload.requests_per_day.toLocaleString()} requests a day (${workload.avg_input_tokens} tokens in, ${workload.avg_output_tokens} out), ${esc(report.pricing.cloud_label)} would cost about <strong>${usd(report.cloud_monthly_usd)}</strong> a month at list price. Servers are priced as ${esc(report.pricing.server_label)}.`
    : 'Add a "workload" to the test suite to estimate capacity and cost.';

  const outputs = report.models.map(m => `
    <details class="fit-outputs">
      <summary>${esc(m.name)}: all ${m.cases.length} answers</summary>
      ${m.cases.map(c => `<div class="fit-case">
        <div class="fit-case-head">
          <span class="badge badge-${c.passed ? 'success' : 'error'}">${c.passed ? 'Pass' : pct(c.score)}</span>
          <strong>${esc(c.task)}</strong>
          <span class="cell-note">${c.latency_s} s</span>
        </div>
        <div class="cell-note">${esc(c.prompt_preview)}</div>
        <pre class="response-text">${esc(c.error || c.output)}</pre>
        ${c.missed.length ? `<div class="cell-note">Missed: ${esc(c.missed.join('; '))}</div>` : ''}
      </div>`).join('')}
    </details>`).join('');

  document.getElementById('fitReport').innerHTML = `
    <div class="section-card">
      <div class="section-title-row">
        <h3>${esc(report.suite_name)}</h3>
        <button class="btn-secondary" onclick="downloadFitReport()">Download JSON</button>
      </div>
      <h4>Best model for each task</h4>
      <p class="api-note">"Fits" means at least ${pct(report.accuracy_threshold)} accuracy, choosing the fastest model that reaches it.</p>
      <div class="table-scroll"><table class="data-table">
        <thead><tr><th>Task</th><th>Model</th><th>Accuracy</th><th>Avg time</th><th>Result</th></tr></thead>
        <tbody>${recommendations}</tbody>
      </table></div>
      <h4>Accuracy by task</h4>
      <div class="table-scroll"><table class="data-table">
        <thead><tr><th>Task</th>${matrixHead}</tr></thead>
        <tbody>${matrix}</tbody>
      </table></div>
      <h4>Speed, capacity and estimated cost</h4>
      <div class="table-scroll"><table class="data-table">
        <thead><tr><th>Model</th><th>Speed</th><th>Typical / slow response</th><th>Memory</th><th>Requests per server per day</th><th>Servers needed</th><th>Server cost per month</th></tr></thead>
        <tbody>${performance}</tbody>
      </table></div>
      <p class="api-note">${costNote}</p>
      <h4>Read this before relying on the results</h4>
      <ul class="fit-caveats">${report.caveats.map(c => `<li>${esc(c)}</li>`).join('')}</ul>
      <h4>Raw answers</h4>
      ${outputs}
    </div>`;
  document.getElementById('fitReport').scrollIntoView({ behavior: 'smooth' });
}

// ── Privacy & Audit ──────────────────────────────────────────────────────────
async function loadGovernance() {
  const isAdmin = state.user && state.user.role === 'admin';
  document.getElementById('governanceAdminOnly').style.display = isAdmin ? 'none' : 'block';
  document.getElementById('governanceContent').style.display = isAdmin ? 'block' : 'none';
  if (!isAdmin) return;
  await Promise.all([loadPolicy(), loadAuditSummary(), loadAuditEvents()]);
}

async function loadPolicy() {
  const policy = await apiFetch('/api/v1/governance/policy');
  document.getElementById('policyCloud').checked = policy.cloud_enabled;
  document.getElementById('policyOnSensitive').value = policy.on_sensitive;
  document.getElementById('policyTerms').value = policy.blocked_terms.join('\n');
  document.getElementById('policyAutoChars').value = policy.auto_cloud_min_chars ?? '';

  const reachable = policy.cloud_server_switch && policy.cloud_configured;
  const banner = document.getElementById('cloudStatus');
  if (reachable && policy.cloud_enabled) {
    banner.className = 'route-banner warn';
    banner.textContent = `Cloud routing is ON. Allowed requests can go to ${policy.cloud_destination}.`;
  } else if (reachable) {
    banner.className = 'route-banner ok';
    banner.textContent = 'All requests stay on this server. Cloud routing is available but switched off below.';
  } else {
    banner.className = 'route-banner ok';
    banner.textContent = 'All requests stay on this server. Cloud routing is disabled in the server configuration and cannot be turned on from this page.';
  }
  document.getElementById('policyCloud').disabled = !reachable;
}

async function savePolicy() {
  const message = document.getElementById('policyMessage');
  const chars = document.getElementById('policyAutoChars').value;
  try {
    await apiFetch('/api/v1/governance/policy', 'PUT', {
      cloud_enabled: document.getElementById('policyCloud').checked,
      on_sensitive: document.getElementById('policyOnSensitive').value,
      blocked_terms: document.getElementById('policyTerms').value.split('\n'),
      auto_cloud_min_chars: chars ? Number(chars) : null,
    });
    message.textContent = 'Saved.';
    await Promise.all([loadPolicy(), loadAuditEvents()]);
  } catch (e) {
    message.textContent = e.message;
  }
}

async function tryScan() {
  const out = document.getElementById('scanResult');
  const text = document.getElementById('scanText').value;
  const { sensitive } = await apiFetch('/api/v1/governance/scan', 'POST', { text });
  const entries = Object.entries(sensitive);
  out.className = entries.length ? 'route-banner warn' : 'route-banner ok';
  out.textContent = entries.length
    ? `Found: ${entries.map(([k, v]) => `${k.replace(/_/g, ' ')} (${v})`).join(', ')}. This would not go to the cloud.`
    : 'Nothing detected. Names and addresses are not detected automatically, so add them as blocked terms.';
}

async function loadAuditSummary() {
  const s = await apiFetch('/api/v1/governance/audit/summary');
  document.getElementById('auditLocal').textContent = s.local.toLocaleString();
  document.getElementById('auditCloud').textContent = s.cloud.toLocaleString();
  document.getElementById('auditBlocked').textContent = s.blocked.toLocaleString();
  document.getElementById('auditSensitive').textContent = s.with_sensitive_data.toLocaleString();
}

async function loadAuditEvents() {
  const route = document.getElementById('auditRoute').value;
  const events = await apiFetch(`/api/v1/governance/audit/events?limit=100${route ? `&route=${encodeURIComponent(route)}` : ''}`);
  const body = document.getElementById('auditBody');
  if (!events.length) {
    body.innerHTML = '<tr><td colspan="7" class="empty-cell">No audit events yet</td></tr>';
    return;
  }
  body.innerHTML = events.map(e => {
    const sensitive = e.sensitive && Object.keys(e.sensitive).length ? Object.keys(e.sensitive).join(', ') : '—';
    const routeClass = e.route === 'cloud' ? 'pending' : e.route === 'blocked' ? 'error' : 'success';
    return `<tr>
      <td>${e.seq}</td>
      <td>${new Date(e.created_at).toLocaleString()}</td>
      <td>${esc(e.event_type)}</td>
      <td>${esc(e.actor_email || '—')}${e.actor_key_prefix ? `<div class="cell-note">${esc(e.actor_key_prefix)}…</div>` : ''}</td>
      <td>${e.route ? `<span class="badge badge-${routeClass}">${esc(e.route)}</span>` : '—'}</td>
      <td>${esc(sensitive)}</td>
      <td>${esc(e.reason || e.status || '—')}</td>
    </tr>`;
  }).join('');
}

async function verifyAudit() {
  const out = document.getElementById('auditVerify');
  out.className = 'route-banner';
  out.textContent = 'Checking…';
  const result = await apiFetch('/api/v1/governance/audit/verify');
  if (result.ok) {
    out.className = 'route-banner ok';
    out.textContent = `Intact: all ${result.events_checked} events verified. Latest hash, to keep off this server for later comparison: ${result.head_hash}`;
  } else {
    out.className = 'route-banner bad';
    out.textContent = `Tampering detected: the chain breaks at event ${result.first_bad_seq}.`;
  }
}

async function exportAudit(format) {
  const resp = await fetch(`/api/v1/governance/audit/export?format=${format}`, {
    headers: { Authorization: `Bearer ${state.token}` },
  });
  if (!resp.ok) { alert(`Export failed (HTTP ${resp.status})`); return; }
  downloadBlob(await resp.blob(), `meshpilot-audit.${format}`);
  await loadAuditEvents();
}
