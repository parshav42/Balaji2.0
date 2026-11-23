const el = id => document.getElementById(id);
const prompt = el('prompt');
const speaker = el('speaker');
const transcriptView = el('chat');
const core = document.querySelector('.core');
const MAX_RECORD_MS = 59_000; // Leave room for MediaRecorder's final audio frame.
const IDLE_LISTEN_MS = 45_000;
const END_OF_TURN_MS = 1_100;
const MIN_VOICE_MS = 150;
const MAX_AUDIO_BYTES = 8 * 1024 * 1024;
const MAX_SPEECH_CHARS = 750;
const recordTypes = new Map([
  ['audio/webm', 'webm'], ['audio/ogg', 'ogg'], ['audio/mp4', 'mp4'],
  ['audio/wav', 'wav'], ['audio/x-wav', 'wav']
]);
const jsonHeaders = {'Content-Type': 'application/json'};
const approvalCards = new Map();
let session = sessionStorage.getItem('balaji_session') || 'web';
let loadingConversation = false;
let conversationNames = {};
try {
  conversationNames = Object.fromEntries(Object.entries(JSON.parse(sessionStorage.getItem('balaji_conversations') || '{}'))
    .filter(([id, name]) => /^chat-[a-z0-9]+$/.test(id) && typeof name === 'string'));
} catch {
  // Older tabs may have stored only the active session.
}
let voiceMode = false;
let voiceApproval = null;
let audioContext = null;
let micPermissionPending = false;
let capture = null;
let chat = null;
let speech = null;
let retryRequest = null;

async function responseError(response) {
  const body = await response.json().catch(() => ({}));
  return Error(typeof body.detail === 'string' ? body.detail : `Request failed (HTTP ${response.status}).`);
}

async function request(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) throw await responseError(response);
  return response.json();
}

function syncConversation() {
  const hasMessages = Boolean(transcriptView.childElementCount);
  transcriptView.hidden = !hasMessages;
  el('dashboard').classList.toggle('in-conversation', hasMessages);
}

function nearChatEnd() {
  return transcriptView.scrollHeight - transcriptView.clientHeight - transcriptView.scrollTop < 72;
}

function updateChat(change) {
  const follow = nearChatEnd();
  change();
  syncConversation();
  if (follow) transcriptView.scrollTop = transcriptView.scrollHeight;
}

function msg(role, text) {
  const message = document.createElement('div');
  message.className = 'msg ' + role;
  message.textContent = text;
  message.setAttribute('role', 'group');
  message.setAttribute('aria-label', role === 'user' ? 'You' : 'BALAJI');
  updateChat(() => transcriptView.append(message));
  return message;
}

function downloadLink(container, path) {
  const url = new URL(path, location.origin);
  if (url.origin !== location.origin || url.pathname !== '/api/files/download') return;
  const link = document.createElement('a');
  link.href = url.href;
  link.textContent = 'Download generated file';
  link.style.display = 'block';
  container.append(link);
}

function showPreparedMessage(draft) {
  const form = el('compose');
  form.elements.recipient.value = draft.recipient;
  form.elements.text.value = draft.body;
  const message = msg('assistant', `Prepared ${draft.channel === 'sms' ? 'SMS' : 'WhatsApp'} message. `);
  const link = document.createElement('a');
  link.href = '#messages';
  link.textContent = 'Review message';
  link.addEventListener('click', event => {
    event.preventDefault();
    openControl('messages');
    form.querySelector('[value="' + (draft.channel === 'sms' ? 'sms' : 'whatsapp') + '"]').focus();
  });
  updateChat(() => message.append(link));
}

function notice(text) {
  const region = el('control-center').open ? 'control-notice' : 'notice';
  el(region).textContent = text;
}

function setStatus(state, detail = '') {
  if (state === 'Ready' && approvalCards.size) state = 'Approval needed';
  if (state === 'Ready' && el('connection').dataset.network === 'offline') state = 'Offline';
  const label = state === 'Ready' ? 'Idle' : state;
  el('voice').dataset.state = state.toLowerCase().replaceAll(' ', '-');
  if (el('orb-state').textContent !== label) el('orb-state').textContent = label;
  if (el('voice-detail').textContent !== detail) el('voice-detail').textContent = detail;
  el('voice-detail').hidden = !detail;
  el('timer').hidden = state !== 'Listening';
}

function showTranscript(text = '') {
  el('transcript').textContent = text ? `Transcript: ${text}` : '';
  el('transcript').hidden = !text;
}

function updateControls() {
  const blocked = Boolean(chat || capture || voiceApproval || loadingConversation);
  el('voice-mode').hidden = !voiceMode;
  el('mic').hidden = Boolean(capture);
  el('mic').disabled = micPermissionPending || blocked;
  el('send').hidden = Boolean(chat || capture || speech);
  el('send').disabled = blocked;
  el('newchat').disabled = Boolean(chat || capture || loadingConversation);
  el('clear-history').disabled = el('newchat').disabled;
  for (const item of el('conversation-list').querySelectorAll('button')) item.disabled = el('newchat').disabled;
  prompt.readOnly = Boolean(capture);
  const stop = el('stop');
  stop.hidden = !(capture || chat || speech);
  const label = capture?.phase === 'recording' ? 'Stop recording'
    : chat ? 'Stop generation' : speech ? 'Stop playback' : 'Cancel recording';
  stop.setAttribute('aria-label', label);
  stop.title = label;
  el('play').hidden = !speech?.needsGesture;
  el('retry-fresh').hidden = el('retry').hidden = !retryRequest || blocked;
}

function endVoiceSession(state = 'Ready', detail = '') {
  voiceMode = false;
  voiceApproval = null;
  if (capture) endCapture(capture);
  if (audioContext && audioContext.state !== 'closed') void audioContext.close().catch(() => {});
  audioContext = null;
  if (chat) chat.allowSpeech = false;
  stopSpeech();
  if (!chat || state !== 'Ready') setStatus(state, detail);
  updateControls();
}

function continueConversation(detail = '') {
  updateControls();
  if (voiceApproval) {
    setStatus('Approval needed', 'Choose Approve or Reject to continue the voice conversation.');
  } else if (voiceMode && el('auto-send').checked && !approvalCards.size && !chat && !capture && !speech && !micPermissionPending) {
    void startRecording(true, detail);
  } else {
    setStatus('Ready', detail);
  }
}

async function refresh() {
  try {
    const [tasks, notes, docs] = await Promise.all([
      request('/api/tasks'), request('/api/memories'), request('/api/documents')
    ]);
    el('tasks').textContent = tasks.map(t => `${t.completed ? '✓' : '□'} ${t.description}`).join('\n') || 'No tasks yet';
    el('memories').textContent = notes.map(x => `• ${x.fact}`).join('\n') || 'No memories yet';
    el('documents').textContent = docs.map(d => `${d.name} (${d.chunks} chunks)`).join('\n') || 'No documents';
  } catch (error) {
    notice(error.message);
  }
}

function actionName(name) {
  const label = name.replaceAll('_', ' ');
  return label.charAt(0).toUpperCase() + label.slice(1);
}

function approvalControls(container, item) {
  const preview = document.createElement('pre');
  preview.textContent = JSON.stringify(item.preview || item.arguments, null, 2);
  const controls = document.createElement('div');
  controls.className = 'action';
  for (const [label, allow] of [['Approve', true], ['Reject', false]]) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = label;
    if (!allow) button.className = 'danger';
    button.dataset.approval = item.id;
    button.disabled = approvalRequests.has(item.id);
    button.addEventListener('click', () => void decideApproval(item, allow));
    controls.append(button);
  }
  container.append(preview, controls);
}

function approval(item) {
  if (approvalCards.has(item.id)) return;
  const container = document.createElement('article');
  container.className = 'msg assistant approval-card';
  const title = document.createElement('h3');
  title.textContent = 'Approve action · ' + actionName(item.tool_name);
  container.append(title);
  approvalControls(container, {...item, session_id: session});
  approvalCards.set(item.id, container);
  updateChat(() => transcriptView.append(container));
  if (!chat && !capture && !speech) setStatus('Approval needed');
}

const approvalRequests = new Set();

function resolveApproval(id, outcome) {
  const card = approvalCards.get(id);
  if (card) {
    approvalCards.delete(id);
    updateChat(() => card.remove());
  }
  const message = outcome.result || outcome.status;
  const reply = msg('assistant', message);
  if (outcome.download) updateChat(() => downloadLink(reply, outcome.download));
  announceReply(message);
  if (voiceApproval === id) {
    voiceApproval = null;
    void speak(message);
  } else if (!chat && !capture && !speech) {
    setStatus('Ready');
  }
}

async function decideApproval(item, allowed) {
  if (approvalRequests.has(item.id)) return;
  approvalRequests.add(item.id);
  const buttons = () => document.querySelectorAll(`[data-approval="${CSS.escape(item.id)}"]`);
  for (const button of buttons()) button.disabled = true;
  try {
    const outcome = await request('/api/approvals/' + item.id, {
      method: 'POST', headers: jsonHeaders, body: JSON.stringify({approved: allowed})
    });
    if (['approved', 'rejected', 'failed'].includes(outcome.status)) {
      if (session === item.session_id) resolveApproval(item.id, outcome);
      if (el('control-center').open) {
        notice(outcome.result || outcome.status);
        if (outcome.download) downloadLink(el('control-notice'), outcome.download);
      }
    } else {
      notice('This action is already being processed.');
    }
  } catch (error) {
    notice('Approval error · ' + error.message);
  } finally {
    approvalRequests.delete(item.id);
    for (const button of buttons()) button.disabled = false;
    void refresh();
    document.dispatchEvent(new Event('balaji:refresh'));
  }
}

function announceReply(text) {
  el('reply-announcement').textContent = text;
}

async function loadConversation() {
  const initialSession = session;
  loadingConversation = true;
  updateControls();
  try {
    const [history, pending] = await Promise.all([
      request('/api/history/' + initialSession), request('/api/approvals/' + initialSession)
    ]);
    if (session !== initialSession) return;
    for (const message of history) msg(message.role, message.content);
    for (const item of pending) approval(item);
  } catch (error) {
    if (session === initialSession) notice(error.message);
  } finally {
    loadingConversation = false;
    updateControls();
  }
}

function saveConversationName(text) {
  if (session === 'web' || conversationNames[session]) return;
  conversationNames[session] = text.slice(0, 66);
  sessionStorage.setItem('balaji_conversations', JSON.stringify(conversationNames));
}

function switchConversation(id) {
  if (chat || capture || loadingConversation) return;
  approvalCards.clear();
  endVoiceSession();
  showTranscript();
  el('notice').textContent = '';
  announceReply('');
  prompt.value = '';
  session = id;
  sessionStorage.setItem('balaji_session', session);
  retryRequest = null;
  updateChat(() => transcriptView.replaceChildren());
  el('control-center').close();
  prompt.focus();
  void loadConversation();
}

function stopSpeech() {
  if (!speech) return;
  const job = speech;
  speech = null;
  job.controller.abort();
  speaker.pause();
  speaker.removeAttribute('src');
  speaker.load();
  if (job.url) URL.revokeObjectURL(job.url);
  updateControls();
}

function finishSpeech() {
  stopSpeech();
  continueConversation();
}

async function playSpeech(job) {
  if (!job || speech !== job) return;
  job.needsGesture = false;
  updateControls();
  try {
    await speaker.play();
  } catch (error) {
    if (speech !== job) return;
    if (error.name === 'NotAllowedError') {
      job.needsGesture = true;
      setStatus('Ready', 'Your browser paused autoplay. Select Play reply to hear BALAJI.');
      updateControls();
    } else {
      endVoiceSession('Error', 'Audio playback failed. Check your browser audio settings and output device.');
    }
  }
}

async function speak(answer) {
  const text = Array.from(answer.replace(/\s+/g, ' ').trim()).slice(0, MAX_SPEECH_CHARS).join('');
  if (!voiceMode || speaker.muted || !text) {
    continueConversation();
    return;
  }
  stopSpeech();
  const job = {controller: new AbortController()};
  speech = job;
  setStatus('Ready', 'Preparing audio…');
  updateControls();
  try {
    const response = await fetch('/api/voice/speak', {
      method: 'POST', headers: jsonHeaders, body: JSON.stringify({text}), signal: job.controller.signal
    });
    if (!response.ok) throw await responseError(response);
    const audio = await response.blob();
    if (speech !== job) return;
    job.url = URL.createObjectURL(audio);
    speaker.src = job.url;
    await playSpeech(job);
  } catch (error) {
    if (speech !== job) return;
    endVoiceSession('Error', error.message);
  }
}

speaker.addEventListener('ended', () => {
  if (!speech || !speaker.ended) return;
  finishSpeech();
});
speaker.addEventListener('playing', () => {
  if (speech && !speaker.paused) setStatus('Speaking');
});
speaker.addEventListener('waiting', () => {
  if (speech && !speaker.paused) setStatus('Ready', 'Buffering audio…');
});
speaker.addEventListener('error', () => {
  if (!speech?.url || !speaker.error) return;
  endVoiceSession('Error', 'The browser could not play the generated audio.');
});

el('play').addEventListener('click', () => void playSpeech(speech));
speaker.volume = Number(el('volume').value) / 100;
el('volume').addEventListener('input', () => {
  speaker.volume = Number(el('volume').value) / 100;
});
el('mute').addEventListener('click', () => {
  speaker.muted = !speaker.muted;
  el('mute').textContent = speaker.muted ? 'Unmute' : 'Mute';
  el('mute').setAttribute('aria-pressed', speaker.muted);
  if (speaker.muted && speech) finishSpeech();
});

function releaseMic(job) {
  clearInterval(job.clock);
  clearTimeout(job.deadline);
  clearTimeout(job.idle);
  cancelAnimationFrame(job.frame);
  job.source?.disconnect();
  job.source = null;
  job.stream?.getTracks().forEach(track => track.stop());
  job.stream = null;
  core.style.removeProperty('--level');
}

function endCapture(job) {
  if (capture !== job) return;
  capture = null;
  job.controller.abort();
  if (job.recorder && job.recorder.state !== 'inactive') job.recorder.stop();
  releaseMic(job);
  updateControls();
}

function prepareAudioContext() {
  const AudioContext = window.AudioContext || window.webkitAudioContext;
  if (!AudioContext || audioContext) return;
  try {
    audioContext = new AudioContext();
    void audioContext.resume().catch(() => {});
  } catch {
    audioContext = null;
  }
}

function observeVoice(job, rms, now) {
  // A sustained signal starts the turn. Brief gaps between words keep it open.
  const threshold = Math.max(0.012, Math.min(0.035, job.noiseFloor * 2.5));
  if (rms >= threshold) {
    job.voiceSince ??= now;
    job.lastVoice = now;
    if (!job.heardVoice && now - job.voiceSince >= MIN_VOICE_MS) {
      job.heardVoice = true;
      clearTimeout(job.idle);
      setStatus('Listening', 'Finish speaking and pause to send.');
    }
  } else {
    job.noiseFloor += (rms - job.noiseFloor) * 0.06;
    if (now - job.lastVoice > 180) job.voiceSince = null;
    if (job.heardVoice && now - job.lastVoice >= END_OF_TURN_MS) finishRecording(job);
  }
}

function updateMeter(job) {
  if (!audioContext) return false;
  const analyser = audioContext.createAnalyser();
  analyser.fftSize = 1024;
  job.source = audioContext.createMediaStreamSource(job.stream);
  job.source.connect(analyser);
  const samples = new Float32Array(analyser.fftSize);
  function frame(now) {
    analyser.getFloatTimeDomainData(samples);
    const rms = Math.sqrt(samples.reduce((sum, sample) => sum + sample * sample, 0) / samples.length);
    if (!document.hidden) core.style.setProperty('--level', Math.min(1, rms * 6).toFixed(2));
    if (job.automatic) observeVoice(job, rms, now);
    if (capture === job && job.phase === 'recording') job.frame = requestAnimationFrame(frame);
  }
  job.frame = requestAnimationFrame(frame);
  return true;
}

function microphoneError(error) {
  const messages = {
    NotAllowedError: 'Microphone permission was denied. Allow microphone access in this site’s browser settings.',
    NotFoundError: 'No microphone was found. Connect one and check your sound settings.',
    NotReadableError: 'The microphone could not be opened. Check your sound settings or close apps using it.',
    NotSupportedError: 'This browser could not record in a supported format. Try a current Chromium or Firefox browser.',
    SecurityError: 'Microphone access is disabled for this page. Open the private HTTPS or localhost dashboard.'
  };
  return messages[error.name] || error.message || 'Microphone recording failed.';
}

async function transcribe(job) {
  if (capture !== job) return;
  job.phase = 'transcribing';
  releaseMic(job);
  setStatus('Transcribing');
  updateControls();
  try {
    const mime = job.recorder.mimeType || job.chunks[0]?.type || '';
    const extension = recordTypes.get(mime.split(';')[0].toLowerCase());
    if (!job.bytes) throw Error('The recording is empty. Speak before pressing Stop.');
    if (!extension) throw Error('The browser produced an unsupported recording format.');
    const formData = new FormData();
    formData.append('file', new Blob(job.chunks, {type: mime}), `recording.${extension}`);
    const {text} = await request('/api/voice/transcribe', {
      method: 'POST', body: formData, signal: job.controller.signal
    });
    if (capture !== job) return;
    if (typeof text !== 'string' || !text.trim()) throw Error('No speech was detected. Try recording again.');
    const transcript = text.trim();
    endCapture(job);
    showTranscript(transcript);
    if (job.automatic && /^(?:end (?:the )?voice session|stop listening)[.!?]*$/i.test(transcript)) {
      endVoiceSession('Ready', 'Voice session ended.');
      return;
    }
    prompt.value = transcript.slice(0, prompt.maxLength);
    setStatus('Ready', transcript.length > prompt.maxLength ? 'The transcript exceeds the chat limit. Edit it before sending.' : '');
    if (!el('review-transcript').checked && transcript.length <= prompt.maxLength) el('form').requestSubmit();
    else prompt.focus();
  } catch (error) {
    if (capture !== job) return;
    endCapture(job);
    if (job.automatic && error.message.startsWith('No speech was detected')) {
      continueConversation('I didn’t catch that. Try speaking again.');
    } else if (job.automatic) {
      endVoiceSession('Error', error.message);
    } else {
      setStatus('Error', error.message);
    }
  }
}

function finishRecording(job) {
  if (capture !== job || job.phase !== 'recording') return;
  job.phase = 'transcribing';
  job.recorder.stop();
  releaseMic(job);
  setStatus('Transcribing');
  updateControls();
}

async function startRecording(automatic = false, detail = '') {
  if (!voiceMode || chat || capture || voiceApproval || loadingConversation || micPermissionPending) return;
  stopSpeech();
  if (!automatic) showTranscript();
  if (!window.isSecureContext) {
    endVoiceSession('Error', 'Microphone access needs a secure page. Open http://127.0.0.1:8000 on this computer.');
    return;
  }
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    endVoiceSession('Error', 'Browser microphone recording is unavailable. Try a current Chromium or Firefox browser.');
    return;
  }
  prepareAudioContext();
  const job = {automatic, phase: 'permission', controller: new AbortController(), chunks: [], bytes: 0,
    noiseFloor: 0, lastVoice: -Infinity, voiceSince: null, heardVoice: false};
  capture = job;
  micPermissionPending = true;
  setStatus('Ready', 'Waiting for microphone permission…');
  updateControls();
  try {
    job.stream = await navigator.mediaDevices.getUserMedia({audio: {
      echoCancellation: true, noiseSuppression: true, autoGainControl: true
    }});
    if (capture !== job) {
      releaseMic(job);
      return;
    }
    const mimeType = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4', 'audio/webm', 'audio/ogg']
      .find(type => MediaRecorder.isTypeSupported?.(type));
    job.recorder = new MediaRecorder(job.stream, mimeType ? {mimeType} : {});
    job.recorder.addEventListener('dataavailable', event => {
      if (capture !== job || !event.data.size) return;
      job.bytes += event.data.size;
      if (job.bytes > MAX_AUDIO_BYTES) {
        endVoiceSession('Error', 'The recording exceeded 8 MiB. Try a shorter recording.');
        return;
      }
      job.chunks.push(event.data);
    });
    job.recorder.addEventListener('stop', () => void transcribe(job));
    const fail = () => {
      if (capture !== job || job.phase !== 'recording') return;
      endVoiceSession('Error', 'The microphone stopped unexpectedly. Check your input device and try again.');
    };
    job.recorder.addEventListener('error', fail);
    job.stream.getAudioTracks().forEach(track => track.addEventListener('ended', fail));
    job.recorder.start(250);
    job.phase = 'recording';
    const started = performance.now();
    function tick() {
      const seconds = Math.floor((performance.now() - started) / 1000);
      el('timer').textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
    }
    tick();
    job.clock = setInterval(tick, 200);
    job.deadline = setTimeout(() => finishRecording(job), MAX_RECORD_MS);
    const hasMeter = updateMeter(job);
    if (job.automatic && !hasMeter) {
      job.automatic = false;
      el('auto-send').checked = false;
      setStatus('Listening', 'Automatic turn detection is unavailable. Select Stop recording when finished.');
    } else if (job.automatic) {
      job.idle = setTimeout(() => {
        if (capture === job && !job.heardVoice) endVoiceSession('Ready', 'Voice session ended after 45 seconds without speech.');
      }, IDLE_LISTEN_MS);
      setStatus('Listening', detail || 'Speak when you’re ready.');
    } else {
      setStatus('Listening');
    }
  } catch (error) {
    if (capture !== job) return;
    endVoiceSession('Error', microphoneError(error));
  } finally {
    micPermissionPending = false;
    updateControls();
    if (job.phase === 'permission' && capture !== job && voiceMode && !chat && !speech) continueConversation();
  }
}

el('mic').addEventListener('click', () => {
  voiceMode = true;
  showTranscript();
  void startRecording(el('auto-send').checked);
});
el('voice-mode').addEventListener('click', () => {
  endVoiceSession('Ready', 'Voice session ended.');
});

el('auto-send').addEventListener('change', () => {
  if (!el('auto-send').checked && capture?.automatic) endCapture(capture);
  if (!chat && !capture && !speech) continueConversation();
  else updateControls();
});

el('stop').addEventListener('click', () => {
  if (chat) {
    endVoiceSession();
    chat.controller.abort();
    el('stop').disabled = true;
  } else if (capture?.phase === 'recording') {
    finishRecording(capture);
  } else if (speech) {
    endVoiceSession('Ready', 'Playback stopped.');
  } else {
    endVoiceSession('Ready', 'Voice session ended.');
  }
});

document.addEventListener('visibilitychange', () => {
  document.documentElement.toggleAttribute('data-inactive', document.hidden);
  if (document.hidden && voiceMode) endVoiceSession('Ready', 'Voice session ended when you left this tab.');
});

async function runChat(text, key) {
  if (chat || capture || voiceApproval || loadingConversation || !text) return;
  stopSpeech();
  retryRequest = null;
  el('notice').textContent = '';
  announceReply('');
  saveConversationName(text);
  const job = {controller: new AbortController(), allowSpeech: voiceMode};
  chat = job;
  updateControls();
  prompt.value = '';
  msg('user', text);
  const answer = msg('assistant waiting', 'BALAJI is thinking…');
  setStatus('Thinking');
  let reply = '';
  let result;
  let reader;
  try {
    const response = await fetch('/api/chat/stream', {
      method: 'POST', headers: {...jsonHeaders, Accept: 'text/event-stream', 'Idempotency-Key': key},
      body: JSON.stringify({message: text, session_id: session}), signal: job.controller.signal
    });
    if (!response.ok) throw await responseError(response);
    if (!response.body) throw Error('This browser could not read the streamed reply.');
    reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (!result) {
      const {done, value} = await reader.read();
      if (done) throw Error('The reply ended before it was complete.');
      buffer += decoder.decode(value, {stream: true});
      const blocks = buffer.split('\n\n');
      buffer = blocks.pop();
      for (const block of blocks) {
        const line = block.split('\n').find(line => line.startsWith('data: '));
        if (!line) continue;
        const update = JSON.parse(line.slice(6));
        if (update.type === 'token') {
          if (el('voice').dataset.state === 'executing') setStatus('Thinking');
          reply += update.text;
          answer.classList.remove('waiting');
          updateChat(() => { answer.textContent = reply; });
        } else if (update.type === 'tool') {
          reply = '';
          const detail = update.tool_name ? actionName(update.tool_name) : 'Running a tool for your request';
          answer.classList.add('waiting');
          updateChat(() => { answer.textContent = detail + '…'; });
          setStatus('Executing', detail);
        } else if (update.type === 'done') {
          result = update;
          break;
        } else if (update.type === 'error') {
          throw Error(update.detail);
        }
      }
    }
    if (job.controller.signal.aborted) return;
    if (result.pending_approval) {
      updateChat(() => answer.remove());
      approval(result.pending_approval);
      if (job.allowSpeech && voiceMode) voiceApproval = result.pending_approval.id;
    } else {
      answer.classList.remove('waiting');
      updateChat(() => { answer.textContent = result.answer; });
    }
    announceReply(result.answer);
    if (result.prepared_message) showPreparedMessage(result.prepared_message);
    setStatus('Ready');
    void refresh();
  } catch (error) {
    answer.classList.remove('waiting');
    if (job.controller.signal.aborted) {
      if (!reply) updateChat(() => answer.remove());
      setStatus('Ready', 'Generation stopped.');
    } else {
      if (!reply) updateChat(() => { answer.textContent = 'Could not complete the reply.'; });
      retryRequest = {text, key};
      endVoiceSession('Error', error.message);
    }
  } finally {
    if (reader) void reader.cancel().catch(() => {});
    chat = null;
    el('stop').disabled = false;
    updateControls();
  }
  if (voiceApproval) {
    // Speak a fixed prompt while the details and approval controls remain in the dashboard.
    void speak('This action needs your approval. Please choose Approve or Reject on the screen.');
  } else if (job.allowSpeech && result) {
    void speak(result.answer);
  } else if (voiceMode) {
    continueConversation(job.controller.signal.aborted ? 'Generation stopped.' : '');
  }
  document.dispatchEvent(new Event('balaji:refresh'));
}

el('form').addEventListener('submit', event => {
  event.preventDefault();
  void runChat(prompt.value.trim(), crypto.randomUUID());
});
el('retry').addEventListener('click', () => {
  const pending = retryRequest;
  if (pending) void runChat(pending.text, pending.key);
});

el('retry-fresh').addEventListener('click', () => {
  const pending = retryRequest;
  if (pending && confirm('Start a new run for this message? Check the result of the first run before retrying an action.')) {
    void runChat(pending.text, crypto.randomUUID());
  }
});

el('document').addEventListener('change', async () => {
  const file = el('document').files[0];
  if (!file) return;
  const body = new FormData();
  body.append('file', file);
  notice(`Indexing ${file.name}…`);
  try {
    const result = await request('/api/documents/upload', {method: 'POST', body});
    notice(`Indexed ${result.chunks} passages from ${result.name}`);
    void refresh();
  } catch (error) {
    notice(error.message);
  } finally {
    el('document').value = '';
  }
});
el('refresh').addEventListener('click', () => void refresh());
el('newchat').addEventListener('click', () => {
  switchConversation('chat-' + crypto.randomUUID().replaceAll('-', '').slice(0, 20));
});

updateControls();
