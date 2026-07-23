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
  setupTabs();
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

function setupTabs() {
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const tab = btn.dataset.tab;
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach(c => c.style.display = 'none');
      btn.classList.add('active');
      document.getElementById(`tab-${tab}`).style.display = 'block';
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

async function signup() {
  const username = document.getElementById('signupUsername').value.trim();
  const email    = document.getElementById('signupEmail').value.trim();
  const pwd      = document.getElementById('signupPassword').value;
  try {
    await apiFetch('/api/v1/auth/register', 'POST', { username, email, password: pwd });
    // Auto-login after signup
    document.getElementById('loginEmail').value = email;
    document.getElementById('loginPassword').value = pwd;
    await login();
  } catch (e) {
    showError(e.message || 'Signup failed');
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

  try {
    const history = await apiFetch('/api/v1/inference/history?limit=10');
    renderHistory(history);
  } catch (e) {
    console.error('History load failed:', e);
  }
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
async function loadDemoModels() {
  try {
    const models = await apiFetch('/api/v1/models');
    const ready  = models.filter(m => m.status === 'ready');
    const sel    = document.getElementById('demoModel');
    sel.innerHTML = ready.length
      ? ready.map(m => `<option value="${m.id}">${m.name} (${m.quant_bits || 'FP32'})</option>`).join('')
      : '<option value="">No ready models — upload one first</option>';
  } catch (e) {
    console.error('Failed to load demo models:', e);
  }
}

async function runDemo() {
  const modelId = document.getElementById('demoModel').value;
  const prompt  = document.getElementById('demoPrompt').value.trim();
  if (!modelId) { alert('Select a model first'); return; }
  if (!prompt)  { alert('Enter a prompt'); return; }

  const btn = document.getElementById('runBtn');
  btn.disabled = true;
  btn.textContent = '⏳ Running...';
  document.getElementById('responseArea').style.display = 'none';

  // Use API key if available, else JWT
  const headers = state.apiKey
    ? { 'X-API-Key': state.apiKey }
    : { 'Authorization': `Bearer ${state.token}` };

  try {
    const t0 = Date.now();
    const resp = await fetch(`${API}/api/v1/inference/sync/${modelId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...headers },
      body: JSON.stringify({
        prompt,
        max_tokens: parseInt(document.getElementById('maxTokens').value),
        temperature: parseFloat(document.getElementById('temperature').value),
      }),
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
  } finally {
    btn.disabled = false;
    btn.textContent = '▶ Run Inference';
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
