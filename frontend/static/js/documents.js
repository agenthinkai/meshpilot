/**
 * MeshPilot Documents — ask questions and get answers from uploaded documents.
 * Admins also manage the documents here.
 */

const DOCUMENT_DEFAULT_MODEL = 'allam-7b';
const DOCUMENT_NOT_FOUND = 'NOT_FOUND';
const DOCUMENT_SLOW_START_MS = 8000;

let documentAskRunning = false;

async function loadDocumentsPage() {
  await Promise.all([loadDocumentModels(), loadDocuments()]);
}

async function loadDocumentModels() {
  const select = document.getElementById('docModel');
  try {
    const models = (await apiFetch('/api/v1/models'))
      .filter(m => m.status === 'ready' && !(m.backend || '').includes('onnx'))
      .sort((a, b) => (b.parameters_b || 0) - (a.parameters_b || 0));
    select.innerHTML = models.map(m => `<option value="${esc(m.id)}">${esc(m.name.replace(/\s*\(.*\)$/, ''))}</option>`).join('')
      || '<option value="">No model ready</option>';
    if (models.some(m => m.id === DOCUMENT_DEFAULT_MODEL)) select.value = DOCUMENT_DEFAULT_MODEL;
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function loadDocuments() {
  const body = document.getElementById('documentsBody');
  try {
    const documents = await apiFetch('/api/v1/documents');
    document.getElementById('docCount').textContent = documents.length
      ? `Searching ${documents.length} ${documents.length === 1 ? 'document' : 'documents'}`
      : 'No documents have been added yet';
    body.innerHTML = documents.map(d => `
      <tr>
        <td dir="auto">${esc(d.title)}</td>
        <td>${d.chunk_count}</td>
        <td>${new Date(d.created_at).toLocaleDateString()}</td>
        <td class="row-actions"><button class="btn-sm" type="button" onclick="deleteDocument('${esc(d.id)}')">Delete</button></td>
      </tr>`).join('') || '<tr><td colspan="4" class="empty-cell">No documents yet</td></tr>';
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function uploadDocuments(input) {
  const files = [...input.files];
  input.value = '';
  let added = 0;
  for (const file of files) {
    const form = new FormData();
    form.append('file', file);
    toast(`Reading ${file.name}…`);
    const resp = await fetch(`${API}/api/v1/documents`, {
      method: 'POST', headers: { Authorization: `Bearer ${state.token}` }, body: form,
    }).catch(() => null);
    if (resp && resp.ok) {
      added += 1;
      continue;
    }
    const data = resp ? await resp.json().catch(() => ({})) : {};
    toast(`${file.name}: ${data.detail || 'The upload failed.'}`, 'error');
  }
  if (added) toast(`Added ${added} ${added === 1 ? 'document' : 'documents'}.`, 'success');
  await loadDocuments();
}

async function deleteDocument(id) {
  const yes = await confirmDialog('Delete this document?', 'Questions will no longer be answered from it.', 'Delete', true);
  if (!yes) return;
  try {
    await apiFetch(`/api/v1/documents/${id}`, 'DELETE');
    await loadDocuments();
  } catch (e) {
    toast(e.message, 'error');
  }
}

function documentSourceLabel(source) {
  return [source.title, source.section, source.page ? `page ${source.page}` : null].filter(Boolean).join(', ');
}

function renderDocumentSources(sources) {
  const area = document.getElementById('docSources');
  area.hidden = !sources.length;
  area.innerHTML = sources.length ? `
    <div class="doc-sources-title">Passages it was given to read</div>
    ${sources.map(s => `
      <details class="doc-source">
        <summary dir="auto">${esc(documentSourceLabel(s))}</summary>
        <div class="doc-source-text" dir="auto">${esc(s.text)}</div>
      </details>`).join('')}` : '';
}

function documentAnswerMessage(text) {
  document.getElementById('docAnswer').innerHTML = `<div class="msg-waiting"><span>${esc(text)}</span></div>`;
}

function documentNotFoundText(question) {
  return /[؀-ۿ]/.test(question)
    ? 'لا تغطي المستندات هذا السؤال.'
    : "The documents don't cover this question.";
}

async function askDocuments() {
  const input = document.getElementById('docQuestion');
  const question = input.value.trim();
  if (!question || documentAskRunning) return;

  const button = document.getElementById('docAskBtn');
  documentAskRunning = true;
  button.disabled = true;
  button.textContent = 'Reading…';
  renderDocumentSources([]);
  documentAnswerMessage('Searching the documents');
  const answerArea = document.getElementById('docAnswer');
  document.getElementById('docResult').hidden = false;

  let answer = '';
  let notFound = false;
  let slowTimer = null;
  const watchForSlowStart = () => {
    clearTimeout(slowTimer);
    slowTimer = setTimeout(() => {
      if (!answer) documentAnswerMessage('Still reading. The first words can take a while if the model has been idle.');
    }, DOCUMENT_SLOW_START_MS);
  };

  try {
    const resp = await fetch(`${API}/api/v1/documents/ask/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${state.token}` },
      body: JSON.stringify({ question, model: document.getElementById('docModel').value || null }),
    });
    if (resp.status === 401) { logout(); return; }
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(typeof data.detail === 'string' ? data.detail : 'The request failed.');
    }

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
        const place = line.match(/^: queue ahead=(\d+)$/);
        if (place) {
          const ahead = Number(place[1]);
          if (ahead) {
            clearTimeout(slowTimer);
            documentAnswerMessage(`${ahead} ${ahead === 1 ? 'request' : 'requests'} ahead of you. This model answers one at a time.`);
          } else {
            documentAnswerMessage('Reading the passages');
            watchForSlowStart();
          }
          continue;
        }
        if (!line.startsWith('data:')) continue;
        const payload = line.slice(5).trim();
        if (payload === '[DONE]') continue;
        const data = JSON.parse(payload);
        if (data.error) throw new Error(data.error.message);
        if (data.sources) {
          notFound = data.not_found;
          renderDocumentSources(data.sources);
          if (!notFound) {
            documentAnswerMessage('Reading the passages');
            watchForSlowStart();
          }
          continue;
        }
        const delta = data.choices && data.choices[0] && data.choices[0].delta && data.choices[0].delta.content;
        if (delta) {
          answer += delta;
          // The model signals a missing answer with a marker, which shouldn't flash on screen.
          if (!DOCUMENT_NOT_FOUND.startsWith(answer.trim())) {
            clearTimeout(slowTimer);
            answerArea.innerHTML = `<div class="msg-body" dir="auto">${renderMarkdown(answer)}</div>`;
          }
        }
      }
    }
    if (notFound || !answer.trim() || answer.includes(DOCUMENT_NOT_FOUND)) {
      renderDocumentSources([]);
      documentAnswerMessage(documentNotFoundText(question));
    }
  } catch (e) {
    renderDocumentSources([]);
    answerArea.innerHTML = `<div class="msg-error" role="alert">${esc(e instanceof TypeError ? "Can't reach the server. Check your connection." : e.message)}</div>`;
  } finally {
    clearTimeout(slowTimer);
    documentAskRunning = false;
    button.disabled = false;
    button.textContent = 'Ask';
  }
}
