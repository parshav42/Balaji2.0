let currentDevice = null;
let accessPoll;
let liveFeed;
let started = false;
let lastDeviceCheck = 0;
let canInstall;
let lastJobEvent;

const post = (path, data, idempotent = false) => request(path, {
  method: 'POST', headers: idempotent ? {...jsonHeaders, 'Idempotency-Key': crypto.randomUUID()} : jsonHeaders,
  body: JSON.stringify(data)
});

function handle(action) {
  return async event => {
    event.preventDefault();
    try {
      await action(event);
    } catch (error) {
      notice(error.message);
      el('notice').scrollIntoView({block: 'nearest'});
    }
  };
}

function rows(id, values, render, empty) {
  const list = el(id);
  list.replaceChildren();
  if (!values.length) {
    const row = document.createElement('li');
    row.textContent = empty;
    list.append(row);
  }
  for (const item of values) {
    const row = document.createElement('li');
    list.append(row);
    render(row, item);
  }
}

function button(label, action) {
  const control = document.createElement('button');
  control.type = 'button';
  control.textContent = label;
  control.addEventListener('click', handle(async event => {
    control.disabled = true;
    try { await action(event); } finally { control.disabled = false; }
  }));
  return control;
}

function setNetwork(online) {
  el('network').hidden = online;
  el('laptop-state').textContent = online ? 'Online · connected' : 'Unreachable';
  if (online && el('voice').dataset.state === 'offline') setStatus('Ready');
  if (!online) {
    el('model-state').textContent = 'Ollama status unavailable';
    el('device-state').textContent = 'Device status unavailable';
    if (voiceMode) endVoiceSession('Offline', 'You can start a new voice session after reconnection.');
    else if (!chat) setStatus('Offline');
  }
}

async function checkAccess() {
  try {
    const status = await request('/api/access');
    currentDevice = status.device;
    const admitted = !status.auth_required || currentDevice?.status === 'approved';
    el('access-panel').hidden = admitted;
    el('workspace').hidden = !admitted;
    if (!admitted) {
      el('enroll').hidden = currentDevice?.status === 'pending';
      el('access-status').textContent = currentDevice?.status === 'pending'
        ? 'Waiting for the laptop owner to approve this device.'
        : 'Enter a one-use pairing code from the laptop owner.';
      accessPoll = setTimeout(checkAccess, 4000);
      return;
    }
    if (started) return;
    started = true;
    const isOwner = currentDevice?.role === 'owner';
    el('invite').hidden = !status.auth_required || !isOwner;
    el('signout').hidden = !status.auth_required;
    el('clear-history').hidden = !isOwner;
    el('session-description').textContent = status.auth_required
      ? `Signed in as ${currentDevice.name}. Session expires ${new Date(currentDevice.expires_at * 1000).toLocaleString()}.`
      : 'Local laptop access. Phone access requires private HTTPS and a paired device.';
    startLiveFeed();
    await Promise.allSettled([loadConversation(), refresh(), refreshPanels()]);
  } catch {
    el('access-status').textContent = 'Laptop unreachable. Reconnecting when it comes back online.';
    el('enroll').hidden = true;
    accessPoll = setTimeout(checkAccess, 5000);
  }
}

el('enroll').addEventListener('submit', async event => {
  event.preventDefault();
  clearTimeout(accessPoll);
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  const submit = form.querySelector('button');
  submit.disabled = true;
  try {
    await post('/api/access/enroll', {...data,
      platform: /Android/i.test(navigator.userAgent) ? 'Android' : /iPhone|iPad/i.test(navigator.userAgent) ? 'iOS' : 'Browser'});
    form.reset();
    await checkAccess();
  } catch (error) {
    el('access-status').textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

el('invite').addEventListener('click', handle(async () => {
  const invite = await post('/api/access/invite', {});
  el('pair-code').textContent = `Pairing code (valid for 10 minutes): ${invite.code}`;
}));
el('signout').addEventListener('click', handle(async () => {
  await post('/api/access/logout', {});
  location.reload();
}));

async function renderDevices() {
  const devices = await request('/api/access/devices');
  const phones = devices.filter(device => ['Android', 'iOS'].includes(device.platform) && device.status === 'approved');
  el('device-state').textContent = phones.length
    ? phones.map(d => `${d.name} · ${d.online ? 'online' : 'offline'}`).join(' / ')
    : 'No paired phone';
  rows('devices-list', devices, (row, device) => {
    const lastSeen = new Date(device.last_seen * 1000).toLocaleString();
    row.textContent = `${device.name} · ${device.platform} · ${device.status} · ${device.online ? 'online' : 'offline'} · Last seen ${lastSeen}`;
    if (device.capabilities.length) row.append(` · ${device.capabilities.join(', ')}`);
    if (currentDevice?.role !== 'owner' || device.role === 'owner' || device.status === 'revoked') return;
    if (device.status === 'pending') row.append(button('Approve device', async () => {
      await post('/api/access/devices/' + device.id, {allow: true});
      await renderDevices();
    }));
    row.append(button(device.status === 'pending' ? 'Deny device' : 'Revoke access', async () => {
      if (!confirm(`Remove access for ${device.name}?`)) return;
      await post('/api/access/devices/' + device.id, {allow: false});
      await renderDevices();
    }));
  }, 'No enrolled browsers');
}

function localDate(timestamp, timezone) {
  return new Intl.DateTimeFormat(undefined, {dateStyle: 'medium', timeStyle: 'short', timeZone: timezone})
    .format(new Date(timestamp * 1000));
}

function renderJobs(data) {
  el('job-timezone').textContent = `Start times use ${data.timezone}.`;
  const next = data.jobs.filter(item => item.state === 'active').sort((a, b) => a.next_run - b.next_run)[0];
  el('next-job').textContent = next ? `${next.name} · ${localDate(next.next_run, next.timezone)}` : 'No scheduled reminders';
  rows('jobs', data.jobs, (row, job) => {
    row.textContent = `${job.name} · ${job.recurrence} · ${job.state}`;
    if (job.state === 'active' || job.state === 'paused') {
      row.append(` · ${localDate(job.next_run, job.timezone)}`);
      for (const action of [job.state === 'active' ? 'pause' : 'resume', 'run', 'cancel']) {
        row.append(button({pause: 'Pause', resume: 'Resume', run: 'Run now', cancel: 'Cancel'}[action], async () => {
          await post('/api/automations/' + job.id, {action});
          renderJobs(await request('/api/automations'));
        }));
      }
    }
  }, 'No automations scheduled');
  rows('job-events', data.events, (row, event) => {
    row.textContent = `${new Date(event.ran_at * 1000).toLocaleString()} · ${event.message}`;
  }, 'No deliveries yet');
  if (data.events[0] && lastJobEvent && data.events[0].id > lastJobEvent) notice(data.events[0].message);
  lastJobEvent = data.events[0]?.id || 0;
}

el('new-job').addEventListener('submit', handle(async event => {
  const form = event.currentTarget;
  await post('/api/automations', Object.fromEntries(new FormData(form)));
  form.reset();
  renderJobs(await request('/api/automations'));
}));

function renderActivity(data) {
  const active = data.runs.find(r => ['RECEIVED', 'PLANNING', 'EXECUTING', 'WAITING_FOR_APPROVAL', 'RESUMING'].includes(r.state));
  el('current-run').textContent = active ? active.state.replaceAll('_', ' ').toLowerCase() : 'Idle';
  el('pending-count').textContent = `${data.pending} pending approval${data.pending === 1 ? '' : 's'}`;
  rows('audit', data.runs, (row, run) => {
    const details = document.createElement('details');
    const heading = document.createElement('summary');
    heading.textContent = `${run.state.replaceAll('_', ' ')} · ${new Date(run.updated_at * 1000).toLocaleString()}`;
    details.append(heading);
    for (const event of data.events.filter(event => event.run_id === run.id).reverse()) {
      const line = document.createElement('p');
      line.textContent = `${event.action} · ${event.status}`;
      details.append(line);
    }
    const title = document.createElement('p');
    title.textContent = `Conversation ${run.session_id}`;
    details.append(title);
    row.append(details);
  }, 'No agent runs yet');
}

async function renderApprovals() {
  const pending = await request('/api/approvals');
  rows('approvals-list', pending, (row, item) => {
    const text = document.createElement('pre');
    text.style.whiteSpace = 'pre-wrap';
    text.textContent = `${item.tool_name}\n${JSON.stringify(item.preview || item.arguments, null, 2)}`;
    row.append(text);
    for (const allowed of [true, false]) {
      row.append(button(allowed ? 'Approve' : 'Reject', async () => {
        const outcome = await post('/api/approvals/' + item.id, {approved: allowed});
        const message = outcome.result || outcome.status;
        notice(message);
        if (voiceApproval === item.id) {
          voiceApproval = null;
          msg('assistant', message);
          void speak(message);
        }
        await Promise.allSettled([renderApprovals(), refresh(), renderGmail()]);
      }));
    }
  }, 'No pending approvals');
}

async function renderGmail() {
  const [gmail, drafts] = await Promise.all([request('/api/gmail'), request('/api/gmail/drafts')]);
  el('gmail-status').textContent = gmail.connected ? `Connected · ${gmail.account}`
    : gmail.configured ? 'Ready for Gmail authorization from the laptop.' : 'Gmail is disabled. Configure OAuth on the laptop.';
  const onLaptop = ['localhost', '127.0.0.1'].includes(location.hostname);
  el('gmail-connect').hidden = !gmail.configured || gmail.connected || currentDevice?.role !== 'owner' || !onLaptop;
  el('gmail-disconnect').hidden = !gmail.connected || currentDevice?.role !== 'owner';
  rows('drafts', drafts, (row, draft) => {
    const details = document.createElement('details');
    const label = document.createElement('summary');
    label.textContent = `${draft.subject} · ${draft.recipient} · ${draft.status}`;
    const content = document.createElement('p');
    content.style.whiteSpace = 'pre-wrap';
    content.textContent = draft.body;
    details.append(label, content);
    if (draft.status === 'draft') details.append(button('Request send approval', () =>
      requestAction('send_gmail_draft', {draft_id: draft.id})));
    row.append(details);
  }, 'No local reply drafts');
}

el('gmail-connect').addEventListener('click', handle(async () => { location.assign((await post('/api/gmail/connect', {})).url); }));
el('gmail-disconnect').addEventListener('click', handle(async () => {
  if (confirm('Disconnect Gmail from BALAJI?')) {
    await post('/api/gmail/disconnect', {});
    await renderGmail();
  }
}));

async function requestAction(name, args) {
  await post('/api/actions/' + name, {arguments: args, session_id: session}, true);
  await renderApprovals();
  location.hash = 'agent-tasks';
}

async function loadProjects() {
  const all = await request('/api/files/projects');
  const menu = el('project');
  menu.replaceChildren();
  for (const name of all) menu.add(new Option(name, name));
  if (!all.length) menu.add(new Option('No approved projects', ''));
}

el('projects-form').addEventListener('submit', handle(async event => {
  const {project, path} = Object.fromEntries(new FormData(event.currentTarget));
  if (!project) return;
  const action = event.submitter?.value || 'list';
  if (action === 'launch') return requestAction('launch_app', {app: 'vscode', project_name: project});
  if (action === 'download') {
    location.assign('/api/files/download?' + new URLSearchParams({project, path}));
    return;
  }
  const {listing} = await request('/api/files?' + new URLSearchParams({project, folder: path}));
  el('file-list').textContent = listing;
}));

el('compose').addEventListener('submit', handle(event => {
  const data = Object.fromEntries(new FormData(event.currentTarget));
  if (!/^\+[1-9][0-9]{7,14}$/.test(data.recipient)) throw Error('Enter a full international number, including + and country code.');
  const text = encodeURIComponent(data.text.trim());
  if (!text) throw Error('Enter a message to prepare.');
  if (event.submitter?.value === 'whatsapp') location.assign(`https://wa.me/${data.recipient.slice(1)}?text=${text}`);
  else {
    const separator = /iPhone|iPad/i.test(navigator.userAgent) ? '&' : '?';
    location.href = `sms:${data.recipient}${separator}body=${text}`;
  }
}));

async function renderMemory(query = '') {
  const data = await request('/api/memory?' + new URLSearchParams({q: query}));
  const owner = currentDevice?.role === 'owner';
  el('memory-categories').replaceChildren();
  if (owner) {
    for (const [category, enabled] of Object.entries(data.categories)) {
      el('memory-categories').append(button(`${enabled ? 'Disable' : 'Enable'} ${category.replaceAll('_', ' ')}`, async () => {
        await post('/api/memory/categories/' + category, {enabled: !enabled});
        await renderMemory(query);
      }));
    }
  }
  rows('memory-list', data.notes, (row, note) => {
    row.textContent = `${note.category.replaceAll('_', ' ')} · ${note.fact}`;
    if (!owner) return;
    row.append(button('Edit', async () => {
      const fact = window.prompt('Edit memory', note.fact);
      if (!fact) return;
      const category = window.prompt('Category: saved_notes, preferences, projects', note.category);
      if (!category) return;
      await request('/api/memory/' + note.id, {method: 'PUT', headers: jsonHeaders, body: JSON.stringify({fact, category})});
      await renderMemory(query);
    }));
    row.append(button('Delete', async () => {
      if (!confirm('Delete this memory?')) return;
      await request('/api/memory/' + note.id, {method: 'DELETE'});
      await renderMemory(query);
    }));
  }, 'No matching notes');
}

el('memory-search').addEventListener('submit', handle(event => renderMemory(new FormData(event.currentTarget).get('q'))));
el('memory-export').addEventListener('click', handle(async () => {
  const data = await request('/api/memory');
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'}));
  const link = document.createElement('a');
  link.href = url;
  link.download = 'balaji-memory.json';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}));
el('clear-history').addEventListener('click', handle(async () => {
  if (!confirm('Delete the saved messages in the current conversation?')) return;
  await request('/api/history/' + session, {method: 'DELETE'});
  el('chat').replaceChildren();
  notice('Conversation history cleared.');
}));

async function refreshPanels() {
  const panels = await Promise.allSettled([renderDevices(), renderApprovals(), renderGmail(),
    renderMemory(new FormData(el('memory-search')).get('q')), loadProjects()]);
  const error = panels.find(item => item.status === 'rejected');
  if (error) notice(error.reason.message);
}

function startLiveFeed() {
  liveFeed = new EventSource('/api/live');
  liveFeed.onmessage = event => {
    const data = JSON.parse(event.data);
    setNetwork(true);
    el('cpu-ram').textContent = `CPU load ${data.laptop.load_1m} · RAM ${data.laptop.ram_used_mib} / ${data.laptop.ram_total_mib} MiB`;
    el('model-state').textContent = `Ollama · ${data.model} · ${data.ollama ? 'ready' : 'unavailable'}`;
    renderActivity(data.activity);
    renderJobs(data.automations);
    if (Date.now() - lastDeviceCheck > 14000) {
      lastDeviceCheck = Date.now();
      void Promise.allSettled([renderDevices(), renderApprovals(), refresh()]);
    }
  };
  liveFeed.onerror = () => {
    setNetwork(false);
    void request('/api/access').then(status => {
      if (status.auth_required && status.device?.status !== 'approved') location.reload();
    }).catch(() => {});
  };
  liveFeed.addEventListener('locked', () => { liveFeed.close(); location.reload(); });
}

document.addEventListener('balaji:refresh', () => void Promise.allSettled([renderApprovals(), renderGmail()]));
el('refresh').addEventListener('click', () => void refreshPanels());
window.addEventListener('offline', () => { if (started) setNetwork(false); });
window.addEventListener('online', () => {
  if (!started) { clearTimeout(accessPoll); void checkAccess(); }
});
window.addEventListener('beforeinstallprompt', event => {
  event.preventDefault();
  canInstall = event;
  el('install').hidden = false;
});
el('install').addEventListener('click', async () => {
  if (!canInstall) return;
  await canInstall.prompt();
  canInstall = null;
  el('install').hidden = true;
});
if ('serviceWorker' in navigator) void navigator.serviceWorker.register('/sw.js').catch(() => {});
void checkAccess();
