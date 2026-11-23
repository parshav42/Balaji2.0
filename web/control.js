let currentDevice = null;
let accessPoll;
let liveFeed;
let started = false;
let lastDeviceCheck = 0;
let lastLiveSignal = 0;
let canInstall;
let lastJobEvent;
let activePanel;
let approvalsRevision = 0;
let approvalsFingerprint;
let jobsFingerprint;
let activityFingerprint;

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
  const state = online ? 'online' : 'offline';
  if (el('connection').dataset.network === state) return;
  el('connection').dataset.network = state;
  el('connection-label').textContent = online ? 'Connected' : 'Offline';
  el('network').textContent = online ? 'Live link established' : 'Laptop unreachable. Waiting to reconnect.';
  el('laptop-state').textContent = online ? 'Online · connected' : 'Unreachable';
  if (online && el('voice').dataset.state === 'offline') setStatus('Ready');
  if (!online) {
    el('model-state').textContent = 'Ollama status unavailable';
    el('device-state').textContent = 'Device status unavailable';
    el('cpu-ram').textContent = '';
    if (voiceMode) endVoiceSession('Offline', 'You can start a new voice session after reconnection.');
    else if (!chat) setStatus('Offline');
  }
}

function openControl(panelId) {
  const dialog = el('control-center');
  const commands = [...el('commands').querySelectorAll('button')];
  const selected = commands.find(command => command.dataset.panel === panelId);
  panelId = selected?.dataset.panel;
  if (activePanel) el(activePanel).hidden = true;
  activePanel = panelId;
  el('palette').hidden = Boolean(selected);
  el('back-control').hidden = !selected;
  el('control-notice').textContent = '';
  if (!dialog.open) dialog.showModal();
  if (selected) {
    const panel = el(panelId);
    panel.hidden = false;
    const heading = panel.querySelector('h2');
    heading.tabIndex = -1;
    heading.focus();
    if (panelId === 'conversations') renderConversations();
    if (panelId === 'devices') void renderDevices().catch(error => notice(error.message));
  } else {
    const search = el('command-search');
    search.value = '';
    search.dispatchEvent(new Event('input'));
    search.focus();
  }
  dialog.querySelector('.drawer-body').scrollTop = 0;
}

function renderConversations() {
  const ids = [...new Set(['web', ...Object.keys(conversationNames).reverse(), session])];
  rows('conversation-list', ids, (row, id) => {
    const control = button(conversationNames[id] || (id === 'web' ? 'Default conversation' : 'New conversation'),
      () => switchConversation(id));
    if (id === session) control.setAttribute('aria-current', 'true');
    row.append(control);
  });
  updateControls();
}

function bindControlCenter() {
  const dialog = el('control-center');
  const search = el('command-search');
  const commands = [...el('commands').querySelectorAll('button')];
  search.addEventListener('input', () => {
    const terms = search.value.trim().toLowerCase().split(/\s+/);
    for (const command of commands) {
      const keywords = `${command.textContent} ${command.dataset.keywords}`.toLowerCase();
      command.hidden = !terms.every(term => keywords.includes(term));
    }
    el('no-commands').hidden = commands.some(command => !command.hidden);
  });
  for (const command of commands) {
    command.addEventListener('click', () => openControl(command.dataset.panel));
  }
  dialog.addEventListener('keydown', event => {
    const visible = commands.filter(command => !command.hidden);
    const index = visible.indexOf(document.activeElement);
    if (el('palette').hidden || (event.target !== search && index === -1)) return;
    if (event.key === 'Enter' && event.target === search && visible.length) {
      event.preventDefault();
      visible[0].click();
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const next = event.key === 'ArrowDown' ? index + 1 : index - 1;
      (visible[next] || search).focus();
    }
  });
  dialog.addEventListener('click', event => {
    if (event.target !== dialog) return;
    const edge = dialog.getBoundingClientRect();
    if (event.clientX < edge.left) dialog.close();
  });
  el('menu').addEventListener('click', () => openControl());
  el('connection').addEventListener('click', () => openControl('devices'));
  el('close-control').addEventListener('click', () => dialog.close());
  el('back-control').addEventListener('click', () => openControl());
  el('integrations').addEventListener('click', event => {
    const link = event.target.closest('a[href^="#"]');
    if (!link) return;
    event.preventDefault();
    openControl(link.hash.slice(1));
  });
  document.addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      openControl();
    }
  });
  const openHash = () => {
    const id = location.hash.slice(1);
    if (commands.some(command => command.dataset.panel === id)) openControl(id);
  };
  window.addEventListener('hashchange', openHash);
  openHash();
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
    bindControlCenter();
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
    if (currentDevice?.role !== 'owner' || device.id === currentDevice.id || device.status === 'revoked') return;
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
  const fingerprint = JSON.stringify(data);
  if (jobsFingerprint === fingerprint) return;
  jobsFingerprint = fingerprint;
  el('job-timezone').textContent = `Start times use ${data.timezone}.`;
  const next = data.jobs.filter(item => item.state === 'active').sort((a, b) => a.next_run - b.next_run)[0];
  el('next-job').textContent = next ? `${next.name} · ${localDate(next.next_run, next.timezone)}` : 'No scheduled reminders';
  rows('jobs', data.jobs, (row, job) => {
    row.textContent = `${job.name} · ${job.recurrence} · ${job.state}`;
    if (job.last_run) row.append(` · Last run ${localDate(job.last_run, job.timezone)}`);
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
  if (data.events[0] && lastJobEvent !== undefined && data.events[0].id > lastJobEvent) {
    notice(data.events[0].message);
    if ('Notification' in window && Notification.permission === 'granted' && 'serviceWorker' in navigator) {
      void navigator.serviceWorker.ready.then(registration =>
        registration.showNotification('BALAJI reminder', {body: data.events[0].message, tag: 'reminder-' + data.events[0].id})
      ).catch(() => {});
    }
  }
  lastJobEvent = data.events[0]?.id || 0;
}

el('new-job').addEventListener('submit', handle(async event => {
  const form = event.currentTarget;
  await post('/api/automations', Object.fromEntries(new FormData(form)));
  form.reset();
  renderJobs(await request('/api/automations'));
}));

function renderActivity(data) {
  const fingerprint = JSON.stringify(data);
  if (activityFingerprint === fingerprint) return;
  activityFingerprint = fingerprint;
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
  const other = data.events.filter(event => event.run_id === null);
  if (other.length) {
    const details = document.createElement('details');
    const heading = document.createElement('summary');
    heading.textContent = 'Device and system activity';
    details.append(heading);
    for (const event of other) {
      const line = document.createElement('p');
      line.textContent = `${event.action.replaceAll('_', ' ')} · ${event.status} · ${new Date(event.created_at * 1000).toLocaleString()}`;
      details.append(line);
    }
    const item = document.createElement('li');
    item.append(details);
    el('audit').prepend(item);
  }
}

async function renderApprovals() {
  const revision = ++approvalsRevision;
  const pending = await request('/api/approvals');
  if (revision !== approvalsRevision) return;
  const fingerprint = JSON.stringify(pending);
  if (fingerprint !== approvalsFingerprint) {
    approvalsFingerprint = fingerprint;
    rows('approvals-list', pending, (row, item) => {
      row.textContent = actionName(item.tool_name);
      approvalControls(row, item);
    }, 'No pending approvals');
  }
  const originSession = session;
  const pendingIds = new Set(pending.map(item => item.id));
  for (const item of pending) {
    if (item.session_id === session && !loadingConversation && !chat) approval(item);
  }
  await Promise.all([...approvalCards.keys()].filter(id => !pendingIds.has(id) && !approvalRequests.has(id)).map(async id => {
    const outcome = await request('/api/approvals/status/' + id);
    if (session === originSession && approvalCards.has(id) && !approvalRequests.has(id)
      && ['approved', 'rejected', 'failed'].includes(outcome.status)) resolveApproval(id, outcome);
  }));
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
  openControl('agent-tasks');
  await renderApprovals();
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

function composeTarget(app, recipient, body, appleMobile) {
  if (!/^\+[1-9][0-9]{7,14}$/.test(recipient)) throw Error('Enter a full international number, including + and country code.');
  if (!body.trim() || body.length > 1000) throw Error('Enter a message of 1–1000 characters.');
  const text = encodeURIComponent(body.trim());
  if (app === 'whatsapp') return `https://wa.me/${recipient.slice(1)}?text=${text}`;
  if (app !== 'sms') throw Error('Choose SMS or WhatsApp.');
  return appleMobile ? `sms:${recipient}` : `sms:${recipient}?body=${text}`;
}

el('compose').addEventListener('submit', handle(event => {
  const data = Object.fromEntries(new FormData(event.currentTarget));
  location.assign(composeTarget(event.submitter?.value || 'sms', data.recipient, data.text,
    /iPhone|iPad|iPod/i.test(navigator.userAgent)));
}));

el('copy-compose').addEventListener('click', handle(async () => {
  const field = el('compose').elements.text;
  if (!field.value.trim()) throw Error('Enter a message to copy.');
  try {
    await navigator.clipboard.writeText(field.value.trim());
    notice('Message copied. Paste it into your messaging app.');
  } catch {
    field.select();
    notice('Select Copy on your device, then paste the message in your messaging app.');
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
  if (chat || capture || loadingConversation) return;
  if (!confirm('Delete the saved messages in the current conversation?')) return;
  loadingConversation = true;
  endVoiceSession();
  try {
    await request('/api/history/' + session, {method: 'DELETE'});
    saveConversationName('');
    renderConversations();
    updateChat(() => transcriptView.replaceChildren(...approvalCards.values()));
    showTranscript();
    announceReply('');
    notice('Conversation history cleared.');
  } finally {
    loadingConversation = false;
    updateControls();
  }
}));

async function refreshPanels() {
  const panels = await Promise.allSettled([renderDevices(), renderApprovals(), renderGmail(),
    renderMemory(new FormData(el('memory-search')).get('q')), loadProjects()]);
  const error = panels.find(item => item.status === 'rejected');
  if (error) notice(error.reason.message);
}

function startLiveFeed() {
  lastLiveSignal = Date.now();
  liveFeed = new EventSource('/api/live');
  liveFeed.onmessage = event => {
    const data = JSON.parse(event.data);
    lastLiveSignal = Date.now();
    setNetwork(true);
    const cpu = data.laptop.cpu_percent == null ? `load ${data.laptop.load_1m}` : `${data.laptop.cpu_percent}%`;
    el('cpu-ram').textContent = `CPU ${cpu} · RAM ${data.laptop.ram_used_mib} / ${data.laptop.ram_total_mib} MiB`;
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

setInterval(() => {
  if (liveFeed && Date.now() - lastLiveSignal > 28000) {
    setNetwork(false);
    liveFeed.close();
    startLiveFeed();
  }
}, 5000);

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
if ('Notification' in window && Notification.permission === 'default') {
  el('notify').hidden = false;
  el('notify').addEventListener('click', handle(async () => {
    const result = await Notification.requestPermission();
    el('notify').hidden = result !== 'default';
    notice(result === 'granted' ? 'Reminder notifications enabled on this browser.' : 'Browser notifications are unavailable.');
  }));
}
void checkAccess();
