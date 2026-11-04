/**
 * MeshPilot Chat — the page employees use day to day.
 * Conversations stay in this browser's storage. The server only ever sees the
 * messages of the request in progress and records a fingerprint of them.
 */

const CHAT_MAX_TOKENS = 1024;
// About 2,500 tokens of history, which leaves room to answer in a 4,096-token context.
const CHAT_HISTORY_CHARS = 9000;
const MAX_SAVED_CHATS = 50;
const CHAT_QUEUE_NOTICE_MS = 6000;

const CHAT_SUGGESTIONS = [
  ['Summarise a document', 'Summarise this document in five bullet points:\n\n'],
  ['Draft an email', 'Draft a short, polite email about: '],
  ['Explain in plain English', 'Explain this in plain English:\n\n'],
];

const chat = {
  ownerId: null,
  chats: [],
  activeId: null,
  models: [],
  controller: null,
  queueTimer: null,
  frame: null,
};

// ── Storage ──────────────────────────────────────────────────────────────────
function chatStorageKey() {
  return `mp_chats:${state.user.id}`;
}

function readSavedChats() {
  try {
    const saved = JSON.parse(localStorage.getItem(chatStorageKey()) || '[]');
    return Array.isArray(saved) ? saved : [];
  } catch {
    return [];
  }
}

function saveChats() {
  chat.chats.sort((a, b) => b.updated - a.updated);
  chat.chats.length = Math.min(chat.chats.length, MAX_SAVED_CHATS);
  try {
    localStorage.setItem(chatStorageKey(), JSON.stringify(chat.chats));
  } catch {
    toast('This browser has no room to save more chats. Delete some to keep new ones.', 'error');
  }
}

function activeChat() {
  return chat.chats.find(c => c.id === chat.activeId) || null;
}

// ── Page ─────────────────────────────────────────────────────────────────────
async function loadChatPage() {
  if (chat.ownerId !== state.user.id) {
    chat.ownerId = state.user.id;
    chat.chats = readSavedChats();
    chat.activeId = null;
  }
  renderChatList();
  renderChatThread();
  await loadChatModels();
  setupComposer();
  document.getElementById('chatInput').focus();
}

async function loadChatModels() {
  const select = document.getElementById('chatModel');
  try {
    const models = await apiFetch('/api/v1/models');
    chat.models = models
      .filter(m => m.status === 'ready' && !(m.backend || '').includes('onnx'))
      .sort((a, b) => (b.parameters_b || 0) - (a.parameters_b || 0));
  } catch (e) {
    toast(e.message, 'error');
    return;
  }
  select.innerHTML = chat.models.map(m => `<option value="${esc(m.id)}">${esc(modelLabel(m.id))}</option>`).join('')
    || '<option value="">No model ready</option>';
  let saved = null;
  try { saved = localStorage.getItem('mp_chat_model'); } catch {}
  if (chat.models.some(m => m.id === saved)) select.value = saved;
  updateComposerState();
}

function modelLabel(id) {
  const model = chat.models.find(m => m.id === id);
  return model ? model.name.replace(/\s*\(.*\)$/, '') : id;
}

function onChatModelChange() {
  try { localStorage.setItem('mp_chat_model', document.getElementById('chatModel').value); } catch {}
}

function toggleChatList(show) {
  document.getElementById('chatShell').classList.toggle('list-open', show);
}

function newChat() {
  if (chat.controller) return;
  chat.activeId = null;
  toggleChatList(false);
  renderChatList();
  renderChatThread();
  document.getElementById('chatInput').focus();
}

function openChat(id) {
  if (chat.controller) return;
  chat.activeId = id;
  toggleChatList(false);
  renderChatList();
  renderChatThread();
}

async function deleteChat(id) {
  if (chat.controller) return;
  chat.chats = chat.chats.filter(c => c.id !== id);
  if (chat.activeId === id) chat.activeId = null;
  saveChats();
  renderChatList();
  renderChatThread();
}

async function deleteAllChats() {
  if (chat.controller) return;
  const yes = await confirmDialog('Delete all chats?', 'This removes every chat saved in this browser. It can\'t be undone.', 'Delete all', true);
  if (!yes) return;
  chat.chats = [];
  chat.activeId = null;
  saveChats();
  renderChatList();
  renderChatThread();
}

// ── Rendering ────────────────────────────────────────────────────────────────
function renderChatList() {
  const items = document.getElementById('chatItems');
  document.getElementById('chatClearAll').style.display = chat.chats.length ? '' : 'none';
  items.innerHTML = chat.chats.map(c => `
    <div class="chat-item ${c.id === chat.activeId ? 'active' : ''}">
      <button class="chat-item-open" type="button" onclick="openChat('${esc(c.id)}')">${esc(c.title)}</button>
      <button class="chat-item-delete" type="button" aria-label="Delete chat" onclick="deleteChat('${esc(c.id)}')"><svg><use href="#i-trash"/></svg></button>
    </div>`).join('');
}

function greeting() {
  const hour = new Date().getHours();
  const part = hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening';
  const name = (state.user.username || '').split(/[\s._-]/)[0];
  return name ? `${part}, ${name.charAt(0).toUpperCase()}${name.slice(1)}.` : `${part}.`;
}

function renderChatThread() {
  const thread = document.getElementById('chatThread');
  const current = activeChat();
  if (!current || !current.messages.length) {
    thread.innerHTML = `
      <div class="chat-empty">
        <h2>${esc(greeting())}</h2>
        <p>What you ask here is answered by your company's own server.</p>
        <div class="chat-suggestions">
          ${CHAT_SUGGESTIONS.map((s, i) => `<button type="button" onclick="useSuggestion(${i})">${esc(s[0])}</button>`).join('')}
        </div>
      </div>`;
    return;
  }
  thread.innerHTML = current.messages.map((m, i) => renderMessage(m, i === current.messages.length - 1)).join('');
  scrollChat(true);
}

function useSuggestion(index) {
  const input = document.getElementById('chatInput');
  input.value = CHAT_SUGGESTIONS[index][1];
  autosizeComposer();
  input.focus();
}

function renderMessage(message, isLast) {
  if (message.role === 'user') {
    return `<div class="msg msg-user"><div class="msg-bubble">${esc(message.content)}</div>${
      message.trimmed ? '<div class="msg-notice">Older messages no longer fit, so the model can\'t see them. Start a new chat for a different topic.</div>' : ''
    }</div>`;
  }
  return `<div class="msg msg-assistant" ${isLast ? 'id="chatLastMessage"' : ''}>${renderAssistantBody(message, isLast)}</div>`;
}

function renderAssistantBody(message, isLast) {
  const waiting = message.pending && !message.content;
  const body = waiting
    ? `<div class="msg-waiting"><span class="dots"><i></i><i></i><i></i></span><span>${message.queued ? 'Waiting its turn. This model answers one request at a time.' : 'Thinking'}</span></div>`
    : `<div class="msg-body">${renderMarkdown(message.content)}${message.pending ? '<span class="caret"></span>' : ''}</div>`;
  const error = message.error ? `
    <div class="msg-error" role="alert">
      <span>${esc(message.error)}</span>
      ${isLast ? '<button class="link-btn" type="button" onclick="retryChat()">Try again</button>' : ''}
    </div>` : '';
  const footer = message.pending || (!message.content && !message.stopped) ? '' : `
    <div class="msg-foot">
      ${message.meta ? renderMessageMeta(message.meta) : ''}
      ${message.stopped ? '<span>Stopped</span>' : ''}
      ${message.content ? '<button class="msg-action" type="button" data-copy-message aria-label="Copy answer"><svg><use href="#i-copy"/></svg> Copy</button>' : ''}
    </div>`;
  return body + error + footer;
}

function renderMessageMeta(meta) {
  const parts = [];
  if (meta.route === 'cloud') parts.push(`<span class="provenance cloud"><i></i>Sent to ${esc(meta.destination || 'the cloud')}</span>`);
  else parts.push('<span class="provenance"><i></i>Stayed on your server</span>');
  if (meta.modelLabel) parts.push(`<span>${esc(meta.modelLabel)}</span>`);
  if (meta.seconds != null) parts.push(`<span class="num">${meta.seconds.toFixed(1)} s</span>`);
  if (meta.tps != null) parts.push(`<span class="num">${meta.tps.toFixed(1)} tok/s</span>`);
  return parts.join('');
}

function updateLastMessage(message) {
  if (chat.frame) return;
  chat.frame = requestAnimationFrame(() => {
    chat.frame = null;
    const el = document.getElementById('chatLastMessage');
    if (el) {
      el.innerHTML = renderAssistantBody(message, true);
      scrollChat();
    }
  });
}

function scrollChat(force) {
  const scroller = document.getElementById('chatScroll');
  const nearBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 140;
  if (force || nearBottom) scroller.scrollTop = scroller.scrollHeight;
}

// ── Markdown ─────────────────────────────────────────────────────────────────
// Everything is escaped first and only a small fixed set of tags is added, so
// nothing a model writes can become live HTML. Links stay plain text on purpose.
function renderInline(text) {
  return esc(text)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>');
}

function renderTextBlock(text) {
  const out = [];
  let list = null;
  let paragraph = [];
  const flushParagraph = () => {
    if (paragraph.length) out.push(`<p>${paragraph.map(renderInline).join('<br>')}</p>`);
    paragraph = [];
  };
  const flushList = () => {
    if (list) out.push(`<${list.tag}>${list.items.map(i => `<li>${renderInline(i)}</li>`).join('')}</${list.tag}>`);
    list = null;
  };
  for (const line of text.split('\n')) {
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    const bullet = line.match(/^\s*[-*•]\s+(.*)$/);
    const number = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (!line.trim()) {
      flushParagraph();  // a list continues across blank lines, as models often write them
    } else if (heading) {
      flushParagraph();
      flushList();
      out.push(`<h4>${renderInline(heading[2])}</h4>`);
    } else if (bullet || number) {
      flushParagraph();
      const tag = bullet ? 'ul' : 'ol';
      if (list && list.tag !== tag) flushList();
      if (!list) list = { tag, items: [] };
      list.items.push((bullet || number)[1]);
    } else {
      flushList();
      paragraph.push(line);
    }
  }
  flushParagraph();
  flushList();
  return out.join('');
}

function renderMarkdown(source) {
  return (source || '').split('```').map((part, i) => {
    if (i % 2 === 0) return renderTextBlock(part);
    const code = part.replace(/^[\w+-]*\n/, '').replace(/\n$/, '');
    return `<div class="code-wrap"><button class="msg-action" type="button" data-copy-code aria-label="Copy code"><svg><use href="#i-copy"/></svg></button><pre><code>${esc(code)}</code></pre></div>`;
  }).join('');
}

// ── Sending ──────────────────────────────────────────────────────────────────
// Only what fits: the newest messages, starting from a user message and strictly
// alternating, which some model chat templates require.
function buildChatContext(messages) {
  const usable = messages.filter(m => !m.pending && m.content && !(m.role === 'assistant' && m.error));
  const kept = [];
  let chars = 0;
  for (let i = usable.length - 1; i >= 0; i--) {
    chars += usable[i].content.length;
    if (kept.length && chars > CHAT_HISTORY_CHARS) break;
    kept.unshift(usable[i]);
  }
  while (kept.length && kept[0].role !== 'user') kept.shift();
  const merged = [];
  for (const m of kept) {
    const last = merged[merged.length - 1];
    if (last && last.role === m.role) last.content += `\n\n${m.content}`;
    else merged.push({ role: m.role, content: m.content });
  }
  return { messages: merged, trimmed: kept.length < usable.length };
}

function updateComposerState() {
  const button = document.getElementById('chatSendBtn');
  const streaming = !!chat.controller;
  button.innerHTML = `<svg><use href="#i-${streaming ? 'stop' : 'send'}"/></svg>`;
  button.setAttribute('aria-label', streaming ? 'Stop' : 'Send');
  button.classList.toggle('stop', streaming);
  button.disabled = !streaming && !chat.models.length;
}

function autosizeComposer() {
  const input = document.getElementById('chatInput');
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 220)}px`;
}

function setupComposer() {
  const input = document.getElementById('chatInput');
  if (input.dataset.ready) return;
  input.dataset.ready = '1';
  input.addEventListener('input', autosizeComposer);
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      sendChatMessage();
    }
  });
  document.getElementById('chatThread').addEventListener('click', e => {
    const button = e.target.closest('[data-copy-message],[data-copy-code]');
    if (!button) return;
    const source = button.hasAttribute('data-copy-code')
      ? button.parentElement.querySelector('code')
      : button.closest('.msg').querySelector('.msg-body');
    copyText(source.innerText);
  });
}

function sendChatMessage() {
  if (chat.controller) {
    chat.controller.abort();
    return;
  }
  const input = document.getElementById('chatInput');
  const text = input.value.trim();
  if (!text || !chat.models.length) return;
  input.value = '';
  autosizeComposer();

  let current = activeChat();
  if (!current) {
    current = { id: crypto.randomUUID(), title: text.slice(0, 60), messages: [], updated: Date.now() };
    chat.chats.unshift(current);
    chat.activeId = current.id;
  }
  current.messages.push({ role: 'user', content: text });
  streamReply(current);
}

function retryChat() {
  const current = activeChat();
  if (!current || chat.controller) return;
  if (current.messages[current.messages.length - 1].role === 'assistant') current.messages.pop();
  streamReply(current);
}

async function streamReply(current) {
  const modelId = document.getElementById('chatModel').value;
  const { messages, trimmed } = buildChatContext(current.messages);
  current.messages[current.messages.length - 1].trimmed = trimmed;
  const reply = { role: 'assistant', content: '', pending: true };
  current.messages.push(reply);
  current.updated = Date.now();
  renderChatList();
  renderChatThread();

  chat.controller = new AbortController();
  updateComposerState();
  chat.queueTimer = setTimeout(() => {
    if (!reply.content) {
      reply.queued = true;
      updateLastMessage(reply);
    }
  }, CHAT_QUEUE_NOTICE_MS);

  const started = performance.now();
  let firstToken = null;
  let usage = null;
  let route = 'local';
  let destination = null;

  try {
    const resp = await fetch('/v1/chat/completions', {
      method: 'POST',
      signal: chat.controller.signal,
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${state.token}` },
      body: JSON.stringify({
        model: modelId, messages, stream: true, max_tokens: CHAT_MAX_TOKENS,
        stream_options: { include_usage: true },
      }),
    });
    if (resp.status === 401) {
      logout();
      return;
    }
    if (!resp.ok) throw new Error(await chatErrorMessage(resp));
    route = resp.headers.get('X-MeshPilot-Route') || route;

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const events = buffer.split('\n\n');
      buffer = events.pop();
      for (const event of events) {
        const line = event.trim();
        if (!line.startsWith('data:')) continue;
        const payload = line.slice(5).trim();
        if (payload === '[DONE]') continue;
        const data = JSON.parse(payload);
        if (data.error) throw new Error(data.error.message);
        usage = data.usage || usage;
        const delta = data.choices && data.choices[0] && data.choices[0].delta && data.choices[0].delta.content;
        if (delta) {
          if (firstToken === null) firstToken = performance.now();
          reply.content += delta;
          updateLastMessage(reply);
        }
      }
    }
    if (!reply.content) reply.error = 'The model returned an empty answer.';
  } catch (e) {
    if (e.name === 'AbortError') reply.stopped = true;
    else reply.error = e instanceof TypeError ? 'Can\'t reach the server. Check your connection.' : e.message;
  } finally {
    clearTimeout(chat.queueTimer);
    chat.controller = null;
    delete reply.pending;
    delete reply.queued;
    if (reply.content && !reply.stopped && !reply.error) {
      const now = performance.now();
      const generating = firstToken === null ? 0 : (now - firstToken) / 1000;
      reply.meta = {
        route, destination, modelLabel: modelLabel(modelId), seconds: (now - started) / 1000,
        tps: usage && usage.completion_tokens && generating > 0.05 ? usage.completion_tokens / generating : null,
      };
    }
    if (state.user) {
      current.updated = Date.now();
      saveChats();
      renderChatList();
      renderChatThread();
      updateComposerState();
    }
  }
}

async function chatErrorMessage(resp) {
  const data = await resp.json().catch(() => ({}));
  const message = data.detail || (data.error && data.error.message);
  if (resp.status === 400 && /can read at most/.test(String(message))) {
    return 'That\'s too long for this model to read at once. Try a shorter section, or start a new chat.';
  }
  if (resp.status === 429) return 'You\'re sending requests quickly. Wait a minute and try again.';
  if (resp.status >= 500) return 'The model is unavailable right now. Try again in a moment.';
  return typeof message === 'string' ? message : `The request failed (HTTP ${resp.status}).`;
}
