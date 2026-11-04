/**
 * MeshPilot People — admin page for employee accounts.
 */

let peopleById = {};

async function loadPeople() {
  try {
    const people = await apiFetch('/api/v1/admin/users');
    peopleById = Object.fromEntries(people.map(p => [p.id, p]));
    renderPeople(people);
  } catch (e) {
    document.getElementById('peopleBody').innerHTML = `<tr><td colspan="5" class="empty-cell">${esc(e.message)}</td></tr>`;
  }
}

function personStatus(person) {
  if (!person.is_active) return '<span class="badge badge-error">Deactivated</span>';
  if (person.must_change_password) return '<span class="badge badge-pending">Invited</span>';
  return '<span class="badge badge-success">Active</span>';
}

function renderPeople(people) {
  document.getElementById('peopleBody').innerHTML = people.map(person => {
    const isMe = person.id === state.user.id;
    const id = esc(person.id);
    return `<tr class="${person.is_active ? '' : 'row-muted'}">
      <td>
        <div class="person-name">${esc(person.username)}${isMe ? ' <span class="tag">You</span>' : ''}</div>
        <div class="cell-note">${esc(person.email)}</div>
      </td>
      <td>
        <select class="input-field compact" aria-label="Role for ${esc(person.username)}" ${isMe ? 'disabled' : ''} onchange="changeRole('${id}', this)">
          <option value="user" ${person.role === 'user' ? 'selected' : ''}>Member</option>
          <option value="admin" ${person.role === 'admin' ? 'selected' : ''}>Admin</option>
        </select>
      </td>
      <td>${personStatus(person)}</td>
      <td>${person.last_login ? new Date(person.last_login).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : 'Never'}</td>
      <td class="row-actions">${isMe ? '' : `
        ${person.is_active ? `<button class="btn-sm" type="button" onclick="resetPassword('${id}')">Reset password</button>` : ''}
        <button class="btn-sm" type="button" onclick="setPersonActive('${id}', ${!person.is_active})">${person.is_active ? 'Deactivate' : 'Reactivate'}</button>`}
      </td>
    </tr>`;
  }).join('');
}

function showTemporaryPassword(title, person, password) {
  return secretDialog(
    title,
    `Send this to ${person.username} over a private channel. They'll choose their own password when they first sign in. It won't be shown again.`,
    password,
    [['Sign-in address', location.origin], ['Email', person.email]],
  );
}

async function addPerson() {
  let created = null;
  const done = await openDialog({
    title: 'Add person',
    text: 'They\'ll get a temporary password to sign in with.',
    confirmLabel: 'Add person',
    fields: [
      { name: 'name', label: 'Name', required: true, autocomplete: 'off' },
      { name: 'email', label: 'Work email', type: 'email', required: true, autocomplete: 'off' },
      { name: 'role', label: 'Role', type: 'select', value: 'user', options: [['user', 'Member: chat and API keys'], ['admin', 'Admin: everything, including this page']] },
    ],
    onConfirm: async values => {
      created = await apiFetch('/api/v1/admin/users', 'POST', values);
    },
  });
  if (!done) return;
  await loadPeople();
  await showTemporaryPassword(`${created.user.username} has been added`, created.user, created.temporary_password);
}

async function resetPassword(id) {
  const person = peopleById[id];
  const yes = await confirmDialog(
    `Reset password for ${person.username}?`,
    'They\'ll be signed out and need a new temporary password. Their API keys keep working.',
    'Reset password',
  );
  if (!yes) return;
  try {
    const result = await apiFetch(`/api/v1/admin/users/${id}/reset-password`, 'POST');
    await loadPeople();
    await showTemporaryPassword('New temporary password', result.user, result.temporary_password);
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function setPersonActive(id, active) {
  const person = peopleById[id];
  if (!active) {
    const yes = await confirmDialog(
      `Deactivate ${person.username}?`,
      'They\'ll be signed out and their API keys will stop working. You can reactivate them later.',
      'Deactivate',
      true,
    );
    if (!yes) return;
  }
  try {
    await apiFetch(`/api/v1/admin/users/${id}`, 'PATCH', { is_active: active });
    toast(`${person.username} ${active ? 'reactivated' : 'deactivated'}.`);
    await loadPeople();
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function changeRole(id, select) {
  const person = peopleById[id];
  const role = select.value;
  try {
    await apiFetch(`/api/v1/admin/users/${id}`, 'PATCH', { role });
    toast(`${person.username} is now ${role === 'admin' ? 'an admin' : 'a member'}.`);
    await loadPeople();
  } catch (e) {
    select.value = person.role;
    toast(e.message, 'error');
  }
}
