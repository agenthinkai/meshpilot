/**
 * MeshPilot Dashboard — Frontend Application
 * Vanilla JS, no framework dependencies.
 */

const API = '';  // same origin; nginx proxies /api/ to FastAPI

// ── State ─────────────────────────────────────────────────────────────────
let state = {
  token: localStorage.getItem('mp_token') || null,
  apiKey: localStorage.getItem('mp_apikey') || null,
  user: null,
  models: [],
  currentPage: 'dashboard',
};

// ── Init ──────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  setupNavigation();
  setInterval(() => {
    if (state.token && state.currentPage === 'dashboard') loadSavings('savingsDash');
  }, 5000);
  if (state.token) {
    showApp();
  } else {
    showPage('auth');
  }
});

function setupNavigation() {
  document.querySelectorAll('.nav-link').forEach(link => {
    link.addEventListener('click', e => {
      e.preventDefault();
      const page = link.dataset.page;
      navigateTo(page);
    });
  });
}

function navigateTo(page) {
  if (!state.token) { logout(); return; }

  state.currentPage = page;
  document.querySelectorAll('.nav-link').forEach(l => {
    l.classList.toggle('active', l.dataset.page === page);
  });
  showPage(page);

  // Load data for the page
  if (page === 'dashboard') loadDashboard();
  else if (page === 'models') loadModels();
  else if (page === 'apikeys') loadAPIKeys();
  else if (page === 'demo') loadDemoModels();
}

function showPage(page) {
  const sidebar = document.querySelector('.sidebar');
  if (sidebar) sidebar.style.display = page === 'auth' ? 'none' : '';
  document.querySelectorAll('.page').forEach(p => p.style.display = 'none');
  const el = document.getElementById(`page-${page}`);
  if (el) el.style.display = 'block';
}

async function showApp() {
  try {
    const me = await apiFetch('/api/v1/auth/me');
    state.user = me;
    document.getElementById('userName').textContent = me.username || me.email;
    document.getElementById('logoutBtn').style.display = 'inline-block';
    document.getElementById('logoutBtn').onclick = logout;
    loadCPUBadge();
    navigateTo('dashboard');
  } catch {
    logout();
  }
}

// ── Auth ──────────────────────────────────────────────────────────────────
async function login() {
  const email = document.getElementById('loginEmail').value.trim();
  const pwd   = document.getElementById('loginPassword').value;
  try {
    const data = await apiFetch('/api/v1/auth/login', 'POST', { email, password: pwd });
    state.token = data.access_token;
    localStorage.setItem('mp_token', state.token);
    hideError();
    showApp();
  } catch (e) {
    showError(e.message || 'Login failed');
  }
}

function logout() {
  state.token = null;
  state.apiKey = null;
  state.user = null;
  localStorage.removeItem('mp_token');
  localStorage.removeItem('mp_apikey');
  showPage('auth');
}

function showError(msg) {
  const el = document.getElementById('authError');
  el.textContent = msg;
  el.style.display = 'block';
}

function hideError() {
  document.getElementById('authError').style.display = 'none';
}

// ── Dashboard ─────────────────────────────────────────────────────────────
async function loadDashboard() {
  try {
    const stats = await apiFetch('/api/v1/metrics/dashboard');
    document.getElementById('statTotal').textContent   = fmt(stats.total_requests);
    document.getElementById('stat24h').textContent     = fmt(stats.requests_24h);
    document.getElementById('statLatency').textContent = stats.avg_latency_ms ? `${stats.avg_latency_ms}ms` : '—';
    document.getElementById('statTPS').textContent     = stats.avg_throughput_tps ? `${stats.avg_throughput_tps}` : '—';
    document.getElementById('statTokens').textContent  = fmt(stats.total_tokens);
    document.getElementById('statModels').textContent  = stats.models_count;
    renderCPUProfile(stats.cpu_profile);
  } catch (e) {
    console.error('Dashboard load failed:', e);
  }

  loadSavings('savingsDash');

  try {
    const history = await apiFetch('/api/v1/inference/history?limit=10');
    renderHistory(history);
  } catch (e) {
    console.error('History load failed:', e);
  }
}

function fmtUsd(v) {
  if (v == null) return '—';
  return v >= 1 ? `$${v.toFixed(2)}` : `$${v.toFixed(v >= 0.01 ? 4 : 6)}`;
}

function fmtDuration(s) {
  if (s == null) return '—';
  if (s < 60) return `${s.toFixed(1)}s`;
  if (s < 3600) return `${(s / 60).toFixed(1)} min`;
  return `${(s / 3600).toFixed(1)} hr`;
}

async function loadSavings(targetId) {
  try {
    renderSavings(targetId, await apiFetch('/api/v1/metrics/savings'));
  } catch (e) {
    console.error('Savings load failed:', e);
  }
}

function renderSavings(targetId, s) {
  const card = (label, kind, big, sub) => `
    <div class="savings-card">
      <div class="savings-head"><span>${label}</span><span class="savings-kind ${kind === 'Estimate' ? 'estimate' : ''}">${kind}</span></div>
      <div class="savings-big">${big}</div>
      <div class="savings-sub">${sub}</div>
    </div>`;
  const cards = [];
  if (s.compare_runs) {
    cards.push(card('Speed', 'Measured', `${s.speedup_x}x faster`,
      `${s.mesh_tps} vs ${s.baseline_tps} tokens/s over ${fmt(s.compare_runs)} run${s.compare_runs === 1 ? '' : 's'}`));
    cards.push(card('Compute time saved', 'Measured', fmtDuration(s.compute_seconds_saved),
      'Time PyTorch would have needed for the same tokens, minus MeshPilot\'s time'));
    if (s.memory_reduction_pct != null) {
      cards.push(card('Memory', 'Measured', `${s.memory_reduction_pct}% less`,
        `${fmtMem(s.mesh_memory_mb)} vs ${fmtMem(s.baseline_memory_mb)}, sampled during generation`));
    }
    cards.push(card('Compute cost per 1M tokens', 'Estimate', fmtUsd(s.mesh_usd_per_m_tokens),
      `vs ${fmtUsd(s.baseline_usd_per_m_tokens)} on PyTorch (${s.compute_cost_reduction_pct}% lower), one request at a time, at $${s.server_usd_per_hour}/hr (${s.server_price_label})`));
  }
  cards.push(card('Tokens kept on-premises', 'Measured', fmt(s.tokens_on_prem),
    'Prompt and generated tokens processed on this server, none sent to a cloud API'));
  cards.push(card('Same tokens on a cloud API', 'Estimate', fmtUsd(s.cloud_usd),
    `${s.cloud_price_label} list price, $${s.cloud_usd_per_m_input} in / $${s.cloud_usd_per_m_output} out per 1M. Local compute so far: ${fmtUsd(s.local_compute_usd)}`));

  const note = s.compare_runs
    ? 'Baseline: the same model in plain PyTorch (FP32) with the same thread count, prompt and token limit, on this machine.'
    : 'Tick Compare on the Live Demo page to measure speed, memory and compute cost.';
  document.getElementById(targetId).innerHTML =
    `<div class="savings-grid">${cards.join('')}</div><div class="savings-note">${note}</div>`;
}

function renderCPUProfile(cpu) {
  if (!cpu) return;
  const grid = document.getElementById('cpuProfile');
  const items = [
    ['Vendor',     cpu.vendor],
    ['Brand',      cpu.brand],
    ['Cores',      cpu.physical_cores],
    ['Threads',    cpu.logical_cores],
    ['AVX-512',    cpu.avx512],
    ['AVX2',       cpu.avx2],
    ['AMX',        cpu.amx],
    ['NEON',       cpu.neon],
    ['Backend',    cpu.recommended_backend],
    ['Quant',      cpu.recommended_quant],
    ['Threads',    cpu.recommended_threads],
    ['RAM (GB)',   cpu.total_ram_gb],
  ];
  grid.innerHTML = items.map(([k, v]) => {
    const cls = v === true ? 'on' : v === false ? 'off' : '';
    const display = v === true ? '✓ Yes' : v === false ? '✗ No' : (v ?? '—');
    return `<div class="cpu-item"><div class="cpu-key">${k}</div><div class="cpu-val ${cls}">${display}</div></div>`;
  }).join('');
}

function renderHistory(logs) {
  const tbody = document.getElementById('historyBody');
  if (!logs || !logs.length) {
    tbody.innerHTML = '<tr><td colspan="7" style="color:var(--text-dim);text-align:center">No inferences yet</td></tr>';
    return;
  }
  tbody.innerHTML = logs.map(l => `
    <tr>
      <td>${l.id.substring(0, 8)}…</td>
      <td>${l.model_id.substring(0, 12)}…</td>
      <td><span class="badge badge-${l.status === 'completed' ? 'success' : l.status === 'failed' ? 'error' : 'pending'}">${l.status}</span></td>
      <td>${l.latency_ms ? l.latency_ms + 'ms' : '—'}</td>
      <td>${l.throughput_tps ? l.throughput_tps + ' t/s' : '—'}</td>
      <td>${l.backend_used || '—'}</td>
      <td>${l.created_at ? new Date(l.created_at).toLocaleTimeString() : '—'}</td>
    </tr>
  `).join('');
}

// ── Models ────────────────────────────────────────────────────────────────
async function loadModels() {
  try {
    const models = await apiFetch('/api/v1/models');
    state.models = models;
    renderModels(models);
  } catch (e) {
    document.getElementById('modelsList').innerHTML = `<p style="color:var(--error)">${e.message}</p>`;
  }
}

function renderModels(models) {
  const grid = document.getElementById('modelsList');
  if (!models || !models.length) {
    grid.innerHTML = '<p style="color:var(--text-dim)">No models yet. Upload one to get started.</p>';
    return;
  }
  grid.innerHTML = models.map(m => `
    <div class="model-card">
      <div class="model-name">${m.name}</div>
      <div class="model-meta">
        ${m.format || 'unknown'} · ${m.quant_bits || 'FP32'} · ${m.file_size_mb ? m.file_size_mb + 'MB' : '?'}
      </div>
      <div class="model-tags">
        <span class="badge badge-${m.status === 'ready' ? 'ready' : m.status === 'error' ? 'error' : 'pending'}">${m.status}</span>
        ${m.is_public ? '<span class="tag">Public</span>' : '<span class="tag">Private</span>'}
        ${m.backend ? `<span class="tag">${m.backend}</span>` : ''}
      </div>
    </div>
  `).join('');
}

function showUploadModal() {
  document.getElementById('uploadModal').style.display = 'flex';
}
function hideUploadModal() {
  document.getElementById('uploadModal').style.display = 'none';
}

function handleFileSelect(input) {
  const file = input.files[0];
  if (file) {
    document.getElementById('fileDropText').textContent = `Selected: ${file.name} (${(file.size / 1024 / 1024).toFixed(1)} MB)`;
    if (!document.getElementById('uploadName').value) {
      document.getElementById('uploadName').value = file.name.replace(/\.(gguf|onnx|pt|pth|bin|safetensors)$/, '');
    }
  }
}

async function uploadModel() {
  const name = document.getElementById('uploadName').value.trim();
  const desc = document.getElementById('uploadDesc').value.trim();
  const file = document.getElementById('fileInput').files[0];
  if (!name || !file) { alert('Name and file are required'); return; }

  const formData = new FormData();
  formData.append('file', file);
  formData.append('name', name);
  if (desc) formData.append('description', desc);

  document.getElementById('uploadProgress').style.display = 'block';
  document.getElementById('progressFill').style.width = '0%';
  document.getElementById('progressText').textContent = 'Uploading...';

  try {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `${API}/api/v1/models`);
    xhr.setRequestHeader('Authorization', `Bearer ${state.token}`);
    xhr.upload.onprogress = e => {
      if (e.lengthComputable) {
        const pct = Math.round(e.loaded / e.total * 100);
        document.getElementById('progressFill').style.width = pct + '%';
        document.getElementById('progressText').textContent = `Uploading ${pct}%...`;
      }
    };
    xhr.onload = () => {
      if (xhr.status === 200 || xhr.status === 201) {
        document.getElementById('progressText').textContent = 'Upload complete! Quantizing...';
        setTimeout(() => { hideUploadModal(); loadModels(); }, 2000);
      } else {
        document.getElementById('progressText').textContent = `Error: ${xhr.statusText}`;
      }
    };
    xhr.send(formData);
  } catch (e) {
    document.getElementById('progressText').textContent = `Error: ${e.message}`;
  }
}

// ── Demo ──────────────────────────────────────────────────────────────────
const COMPARE_AVAILABLE_MODEL_IDS = ['benchmark-qwen25-05b'];

async function loadDemoModels() {
  try {
    const models = await apiFetch('/api/v1/models');
    const ready  = models.filter(m => m.status === 'ready');
    const sel    = document.getElementById('demoModel');
    sel.innerHTML = ready.length
      ? ready.map(m => `<option value="${m.id}">${m.name} (${m.quant_bits || 'FP32'})</option>`).join('')
      : '<option value="">No ready models — upload one first</option>';
    sel.onchange = updateCompareAvailability;
    updateCompareAvailability();
  } catch (e) {
    console.error('Failed to load demo models:', e);
  }
}

function updateCompareAvailability() {
  const modelId = document.getElementById('demoModel').value;
  const available = COMPARE_AVAILABLE_MODEL_IDS.includes(modelId);
  const checkbox = document.getElementById('compareMode');
  const note = document.getElementById('compareNote');
  checkbox.disabled = !available;
  if (!available) checkbox.checked = false;
  note.textContent = available
    ? 'Runs the exact same model through MeshPilot (llama.cpp, INT4) and plain PyTorch (FP32) side by side, live.'
    : 'Compare is only wired up for Qwen2.5-0.5B-Instruct right now — select it above to enable.';
  onCompareToggle();
}

function onCompareToggle() {
  const compare = document.getElementById('compareMode').checked;
  document.getElementById('responseArea').style.display = 'none';
  document.getElementById('compareArea').style.display = 'none';
}

async function runDemo() {
  const modelId = document.getElementById('demoModel').value;
  const prompt  = document.getElementById('demoPrompt').value.trim();
  if (!modelId) { alert('Select a model first'); return; }
  if (!prompt)  { alert('Enter a prompt'); return; }

  const compareMode = document.getElementById('compareMode').checked;
  const btn = document.getElementById('runBtn');
  btn.disabled = true;
  btn.textContent = '⏳ Running...';
  document.getElementById('responseArea').style.display = 'none';
  document.getElementById('compareArea').style.display = 'none';

  const headers = state.apiKey
    ? { 'X-API-Key': state.apiKey }
    : { 'Authorization': `Bearer ${state.token}` };

  const body = JSON.stringify({
    prompt,
    max_tokens: parseInt(document.getElementById('maxTokens').value),
    temperature: parseFloat(document.getElementById('temperature').value),
  });

  try {
    if (compareMode) {
      await runCompare(modelId, headers, body);
    } else {
      await runSingle(modelId, headers, body);
    }
  } finally {
    btn.disabled = false;
    btn.textContent = '▶ Run Inference';
  }
}

async function runSingle(modelId, headers, body) {
  try {
    const resp = await fetch(`${API}/api/v1/inference/sync/${modelId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...headers },
      body,
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || resp.statusText);

    document.getElementById('responseText').textContent = data.text || '(empty response)';
    document.getElementById('responseMeta').textContent =
      `${data.latency_ms}ms · ${data.throughput_tps} t/s · ${data.completion_tokens} tokens · ${data.backend_used}`;
    document.getElementById('responseArea').style.display = 'block';
  } catch (e) {
    document.getElementById('responseText').textContent = `Error: ${e.message}`;
    document.getElementById('responseMeta').textContent = '';
    document.getElementById('responseArea').style.display = 'block';
  }
}

function fmtSeconds(ms) {
  if (ms == null) return '—';
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
}

function fmtMem(mb) {
  if (mb == null) return null;
  return mb >= 1024 ? `${(mb / 1024).toFixed(2)} GB` : `${Math.round(mb)} MB`;
}

function setupClip(id, text) {
  const el = document.getElementById(id);
  const btn = document.getElementById(id.replace('Text', 'ExpandBtn'));
  el.textContent = text;
  el.classList.remove('expanded');
  requestAnimationFrame(() => {
    const overflowing = el.scrollHeight > el.clientHeight + 4;
    el.classList.toggle('clipped', overflowing);
    btn.style.display = overflowing ? 'block' : 'none';
    btn.textContent = 'Show full response ▾';
  });
}

function toggleExpand(which) {
  const id = which === 'with' ? 'withText' : 'withoutText';
  const el = document.getElementById(id);
  const btn = document.getElementById(id.replace('Text', 'ExpandBtn'));
  const nowExpanded = el.classList.toggle('expanded');
  el.classList.toggle('clipped', !nowExpanded);
  btn.textContent = nowExpanded ? 'Show less ▴' : 'Show full response ▾';
}

async function runCompare(modelId, headers, body) {
  document.getElementById('compareArea').style.display = 'block';
  document.getElementById('speedupBanner').style.display = 'none';
  document.getElementById('statCards').style.display = 'none';
  document.getElementById('chartBlock').style.display = 'none';
  document.getElementById('withText').textContent = 'running…';
  document.getElementById('withoutText').textContent = 'running… (plain PyTorch is much slower — this is expected)';
  document.getElementById('withExpandBtn').style.display = 'none';
  document.getElementById('withoutExpandBtn').style.display = 'none';

  try {
    const resp = await fetch(`${API}/api/v1/inference/compare/${modelId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...headers },
      body,
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || resp.statusText);

    const w = data.with_meshpilot || {};
    const wo = data.without_meshpilot || {};

    setupClip('withText', w.error ? `Error: ${w.error}` : (w.text || '(empty response)'));
    setupClip('withoutText', wo.error ? `Error: ${wo.error}` : (wo.text || '(empty response)'));

    if (!w.error && !wo.error) {
      // Stat cards
      document.getElementById('statWithLatency').textContent = fmtSeconds(w.wall_clock_ms);
      document.getElementById('statWithTps').textContent = `${w.throughput_tps} tok/s · ${w.completion_tokens} tokens`;
      document.getElementById('statWithoutLatency').textContent = fmtSeconds(wo.wall_clock_ms);
      document.getElementById('statWithoutTps').textContent = `${wo.throughput_tps} tok/s · ${wo.completion_tokens} tokens`;
      document.getElementById('statWithMem').textContent = fmtMem(w.memory_mb) ? `📊 ${fmtMem(w.memory_mb)} RAM (live)` : '';
      document.getElementById('statWithoutMem').textContent = fmtMem(wo.memory_mb) ? `📊 ${fmtMem(wo.memory_mb)} RAM (live)` : '';
      document.getElementById('statCards').style.display = 'grid';

      // Bar charts (width % relative to the larger of the two values)
      const maxLatency = Math.max(w.wall_clock_ms, wo.wall_clock_ms) || 1;
      const maxTps = Math.max(w.throughput_tps, wo.throughput_tps) || 1;
      document.getElementById('chartBlock').style.display = 'block';
      // Reset to 0 first so the width transition animates on every run
      const barIds = ['barLatencyWith', 'barLatencyWithout', 'barTpsWith', 'barTpsWithout'];
      const hasMemory = w.memory_mb != null && wo.memory_mb != null;
      document.getElementById('memoryChartRow').style.display = hasMemory ? 'block' : 'none';
      if (hasMemory) barIds.push('barMemWith', 'barMemWithout');
      barIds.forEach(id => { document.getElementById(id).style.width = '0%'; });

      requestAnimationFrame(() => {
        document.getElementById('barLatencyWith').style.width = `${(w.wall_clock_ms / maxLatency) * 100}%`;
        document.getElementById('barLatencyWithout').style.width = `${(wo.wall_clock_ms / maxLatency) * 100}%`;
        document.getElementById('barTpsWith').style.width = `${(w.throughput_tps / maxTps) * 100}%`;
        document.getElementById('barTpsWithout').style.width = `${(wo.throughput_tps / maxTps) * 100}%`;
        if (hasMemory) {
          const maxMem = Math.max(w.memory_mb, wo.memory_mb) || 1;
          document.getElementById('barMemWith').style.width = `${(w.memory_mb / maxMem) * 100}%`;
          document.getElementById('barMemWithout').style.width = `${(wo.memory_mb / maxMem) * 100}%`;
        }
      });
      document.getElementById('barLatencyWithVal').textContent = fmtSeconds(w.wall_clock_ms);
      document.getElementById('barLatencyWithoutVal').textContent = fmtSeconds(wo.wall_clock_ms);
      document.getElementById('barTpsWithVal').textContent = `${w.throughput_tps} t/s`;
      document.getElementById('barTpsWithoutVal').textContent = `${wo.throughput_tps} t/s`;
      if (hasMemory) {
        document.getElementById('barMemWithVal').textContent = fmtMem(w.memory_mb);
        document.getElementById('barMemWithoutVal').textContent = fmtMem(wo.memory_mb);
      }
    }

    if (data.speedup_x) {
      const banner = document.getElementById('speedupBanner');
      let text = `⚡ MeshPilot was ${data.speedup_x}x faster`;
      if (data.memory_reduction_pct) text += ` and used ${data.memory_reduction_pct}% less memory`;
      text += ` — same model, same prompt, same machine`;
      banner.textContent = text;
      banner.style.display = 'block';
    }
    await loadSavings('savingsCompare');
  } catch (e) {
    setupClip('withText', `Error: ${e.message}`);
    setupClip('withoutText', '');
  }
}

// ── API Keys ──────────────────────────────────────────────────────────────
async function loadAPIKeys() {
  try {
    const keys = await apiFetch('/api/v1/auth/api-keys');
    const tbody = document.getElementById('apiKeysBody');
    if (!keys || !keys.length) {
      tbody.innerHTML = '<tr><td colspan="5" style="color:var(--text-dim);text-align:center">No API keys yet</td></tr>';
      return;
    }
    tbody.innerHTML = keys.map(k => `
      <tr>
        <td>${k.name}</td>
        <td><code>${k.prefix}…</code></td>
        <td>${fmt(k.request_count || 0)}</td>
        <td>${k.last_used_at ? new Date(k.last_used_at).toLocaleDateString() : 'Never'}</td>
        <td><button class="btn-sm" onclick="revokeKey('${k.id}')">Revoke</button></td>
      </tr>
    `).join('');
  } catch (e) {
    console.error('Failed to load API keys:', e);
  }
}

async function createAPIKey() {
  const name = prompt('API key name (e.g. "production-server"):');
  if (!name) return;
  try {
    const data = await apiFetch('/api/v1/auth/api-keys', 'POST', { name });
    state.apiKey = data.key;
    localStorage.setItem('mp_apikey', data.key);
    document.getElementById('newKeyValue').textContent = data.key;
    document.getElementById('newKeyBanner').style.display = 'flex';
    loadAPIKeys();
  } catch (e) {
    alert(`Failed to create key: ${e.message}`);
  }
}

async function revokeKey(id) {
  if (!confirm('Revoke this API key?')) return;
  try {
    await apiFetch(`/api/v1/auth/api-keys/${id}`, 'DELETE');
    loadAPIKeys();
  } catch (e) {
    alert(`Failed to revoke: ${e.message}`);
  }
}

function copyKey() {
  const key = document.getElementById('newKeyValue').textContent;
  navigator.clipboard.writeText(key).then(() => alert('Copied!'));
}

// ── CPU Badge ─────────────────────────────────────────────────────────────
async function loadCPUBadge() {
  try {
    const stats = await apiFetch('/api/v1/metrics/dashboard');
    const cpu = stats.cpu_profile;
    if (cpu) {
      document.getElementById('cpuBadge').textContent =
        `${cpu.recommended_backend} · ${cpu.recommended_quant}`;
    }
  } catch {}
}

// ── Utilities ─────────────────────────────────────────────────────────────
async function apiFetch(path, method = 'GET', body = null) {
  const headers = { 'Content-Type': 'application/json' };
  if (state.token) headers['Authorization'] = `Bearer ${state.token}`;
  if (state.apiKey) headers['X-API-Key'] = state.apiKey;

  const opts = { method, headers };
  if (body) opts.body = JSON.stringify(body);

  const resp = await fetch(`${API}${path}`, opts);
  if (resp.status === 401) {
    logout();
    throw new Error('Session expired — please log in again');
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
  return data;
}

function fmt(n) {
  if (n === null || n === undefined) return '—';
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
  if (n >= 1_000)     return (n / 1_000).toFixed(1) + 'K';
  return String(n);
}
