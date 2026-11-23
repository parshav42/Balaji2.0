const el = id => document.getElementById(id);
const prompt = el('prompt');
const speaker = el('speaker');
const MAX_RECORD_MS = 59_000; // Leave room for MediaRecorder's final audio frame.
const MAX_AUDIO_BYTES = 8 * 1024 * 1024;
const MAX_SPEECH_CHARS = 750;
const recordTypes = new Map([
  ['audio/webm', 'webm'], ['audio/ogg', 'ogg'], ['audio/mp4', 'mp4'],
  ['audio/wav', 'wav'], ['audio/x-wav', 'wav']
]);
const jsonHeaders = {'Content-Type': 'application/json'};
const approvalIds = new Set();
let session = sessionStorage.getItem('balaji_session') || 'web';
let voiceMode = false;
let micPermissionPending = false;
let capture = null;
let chat = null;
let speech = null;

async function responseError(response) {
  const body = await response.json().catch(() => ({}));
  return Error(typeof body.detail === 'string' ? body.detail : `Request failed (HTTP ${response.status}).`);
}

async function request(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) throw await responseError(response);
  return response.json();
}

function msg(role, text) {
  const message = document.createElement('div');
  message.className = 'msg ' + role;
  message.textContent = text;
  el('chat').append(message);
  scrollChat();
  return message;
}

function scrollChat() {
  el('chat').scrollTop = el('chat').scrollHeight;
}

function notice(text) {
  el('notice').textContent = text;
}

function setStatus(state, detail = '') {
  document.querySelector('.console').dataset.state = state.toLowerCase();
  el('status').textContent = state;
  el('voice-detail').textContent = detail;
  el('voice-detail').hidden = !detail;
  el('meter').hidden = state !== 'Listening' && state !== 'Speaking';
  el('timer').hidden = state !== 'Listening';
}

function showTranscript(text = '') {
  el('transcript').textContent = text ? `Transcript: ${text}` : '';
  el('transcript').hidden = !text;
}

function updateControls() {
  el('voice-mode').setAttribute('aria-pressed', voiceMode);
  el('voice-mode').textContent = `Voice Mode: ${voiceMode ? 'On' : 'Off'}`;
  el('voice-settings').hidden = !voiceMode;
  el('mic').hidden = !voiceMode;
  el('mic').disabled = micPermissionPending || Boolean(chat || capture);
  el('send').disabled = Boolean(chat || capture);
  el('newchat').disabled = Boolean(chat || capture);
  prompt.readOnly = Boolean(capture);
  el('stop').hidden = !(capture || chat || speech);
  el('stop').textContent = capture?.phase === 'recording' ? 'Stop recording'
    : chat ? 'Stop generation' : speech ? 'Stop playback' : 'Cancel';
  el('play').hidden = !speech?.needsGesture;
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

function approval(p) {
  if (approvalIds.has(p.id)) return;
  approvalIds.add(p.id);
  const originSession = session;
  const container = msg('assistant', `Permission required: ${p.tool_name}\n${JSON.stringify(p.arguments)}`);
  const controls = document.createElement('div');
  controls.className = 'action';
  for (const [label, allow] of [['Approve', true], ['Reject', false]]) {
    const button = document.createElement('button');
    button.textContent = label;
    if (!allow) button.className = 'danger';
    button.onclick = async () => {
      for (const child of controls.children) child.disabled = true;
      try {
        const result = await request('/api/approvals/' + p.id, {
          method: 'POST', headers: jsonHeaders, body: JSON.stringify({approved: allow})
        });
        if (session === originSession) msg('assistant', result.result || result.status);
        void refresh();
      } catch (error) {
        for (const child of controls.children) child.disabled = false;
        if (session === originSession) notice('Approval error: ' + error.message);
      }
    };
    controls.append(button);
  }
  container.append(controls);
  scrollChat();
}

async function loadConversation() {
  const initialSession = session;
  const initialMessages = el('chat').childElementCount;
  try {
    const [history, pending] = await Promise.all([
      request('/api/history/' + initialSession), request('/api/approvals/' + initialSession)
    ]);
    if (session !== initialSession) return;
    if (history.length && !chat && el('chat').childElementCount === initialMessages) {
      el('chat').replaceChildren();
      for (const message of history) msg(message.role, message.content);
    }
    for (const item of pending) approval(item);
  } catch (error) {
    if (session === initialSession) notice(error.message);
  }
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

async function playSpeech(job) {
  if (speech !== job) return;
  job.needsGesture = false;
  updateControls();
  try {
    await speaker.play();
    if (speech === job) setStatus('Speaking');
  } catch (error) {
    if (speech !== job) return;
    if (error.name === 'NotAllowedError') {
      job.needsGesture = true;
      setStatus('Ready', 'Your browser paused autoplay. Select Play reply to hear BALAJI.');
      updateControls();
    } else {
      stopSpeech();
      setStatus('Error', 'Audio playback failed. Check your browser audio settings and output device.');
    }
  }
}

async function speak(answer) {
  const text = Array.from(answer.replace(/\s+/g, ' ').trim()).slice(0, MAX_SPEECH_CHARS).join('');
  if (!voiceMode || speaker.muted || !text) return;
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
    stopSpeech();
    setStatus('Error', error.message);
  }
}

speaker.addEventListener('ended', () => {
  if (!speech || !speaker.ended) return;
  stopSpeech();
  setStatus('Ready');
});
speaker.addEventListener('error', () => {
  if (!speech?.url || !speaker.error) return;
  stopSpeech();
  setStatus('Error', 'The browser could not play the generated audio.');
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
  if (speaker.muted && speech) {
    stopSpeech();
    setStatus('Ready');
  }
});

function releaseMic(job) {
  clearInterval(job.clock);
  clearTimeout(job.deadline);
  cancelAnimationFrame(job.frame);
  job.stream?.getTracks().forEach(track => track.stop());
  job.stream = null;
  if (job.context && job.context.state !== 'closed') void job.context.close().catch(() => {});
  job.context = null;
  el('meter').style.removeProperty('--level');
}

function endCapture(job) {
  if (capture !== job) return;
  capture = null;
  job.controller.abort();
  if (job.recorder && job.recorder.state !== 'inactive') job.recorder.stop();
  releaseMic(job);
  updateControls();
}

function updateMeter(job) {
  const AudioContext = window.AudioContext || window.webkitAudioContext;
  if (!AudioContext) return;
  try {
    job.context = new AudioContext();
    const analyser = job.context.createAnalyser();
    analyser.fftSize = 256;
    job.context.createMediaStreamSource(job.stream).connect(analyser);
    void job.context.resume().catch(() => {});
    const samples = new Uint8Array(analyser.fftSize);
    function frame() {
      analyser.getByteTimeDomainData(samples);
      const rms = Math.sqrt(samples.reduce((sum, sample) => sum + ((sample - 128) / 128) ** 2, 0) / samples.length);
      el('meter').style.setProperty('--level', Math.min(1, 0.14 + rms * 4));
      job.frame = requestAnimationFrame(frame);
    }
    frame();
  } catch {
    // Recording and the timer still work if Web Audio is unavailable.
  }
}

function microphoneError(error) {
  const messages = {
    NotAllowedError: 'Microphone permission was denied. Allow microphone access in this site’s browser settings.',
    NotFoundError: 'No microphone was found. Connect one and check your sound settings.',
    NotReadableError: 'The microphone could not be opened. Check your sound settings or close apps using it.',
    NotSupportedError: 'This browser could not record in a supported format. Try a current Chromium or Firefox browser.',
    SecurityError: 'Microphone access is disabled for this page. Open BALAJI on localhost.'
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
    prompt.value = transcript.slice(0, prompt.maxLength);
    setStatus('Ready', transcript.length > prompt.maxLength ? 'The transcript exceeds the chat limit. Edit it before sending.' : '');
    if (el('auto-send').checked && transcript.length <= prompt.maxLength) el('form').requestSubmit();
    else prompt.focus();
  } catch (error) {
    if (capture !== job) return;
    endCapture(job);
    setStatus('Error', error.message);
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

async function startRecording() {
  if (!voiceMode || chat || capture || micPermissionPending) return;
  stopSpeech();
  showTranscript();
  if (!window.isSecureContext) {
    setStatus('Error', 'Microphone access needs a secure page. Open http://127.0.0.1:8000 on this computer.');
    return;
  }
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    setStatus('Error', 'Browser microphone recording is unavailable. Try a current Chromium or Firefox browser.');
    return;
  }
  const job = {phase: 'permission', controller: new AbortController(), chunks: [], bytes: 0};
  capture = job;
  micPermissionPending = true;
  setStatus('Ready', 'Waiting for microphone permission…');
  updateControls();
  try {
    job.stream = await navigator.mediaDevices.getUserMedia({audio: true});
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
        endCapture(job);
        setStatus('Error', 'The recording exceeded 8 MiB. Try a shorter recording.');
        return;
      }
      job.chunks.push(event.data);
    });
    job.recorder.addEventListener('stop', () => void transcribe(job));
    const fail = () => {
      if (capture !== job || job.phase !== 'recording') return;
      endCapture(job);
      setStatus('Error', 'The microphone stopped unexpectedly. Check your input device and try again.');
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
    setStatus('Listening');
    updateMeter(job);
  } catch (error) {
    if (capture !== job) return;
    endCapture(job);
    setStatus('Error', microphoneError(error));
  } finally {
    micPermissionPending = false;
    updateControls();
  }
}

el('mic').addEventListener('click', () => void startRecording());
el('voice-mode').addEventListener('click', () => {
  voiceMode = !voiceMode;
  if (!voiceMode) {
    if (capture) endCapture(capture);
    stopSpeech();
    showTranscript();
    if (chat) chat.allowSpeech = false;
  }
  if (!chat) setStatus('Ready');
  updateControls();
});

el('stop').addEventListener('click', () => {
  if (chat) {
    chat.controller.abort();
    el('stop').disabled = true;
  } else if (capture?.phase === 'recording') {
    finishRecording(capture);
  } else {
    if (capture) endCapture(capture);
    stopSpeech();
    setStatus('Ready');
  }
});

el('form').addEventListener('submit', async event => {
  event.preventDefault();
  const text = prompt.value.trim();
  if (chat || capture || !text) return;
  stopSpeech();
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
      method: 'POST', headers: {...jsonHeaders, Accept: 'text/event-stream'},
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
          reply += update.text;
          answer.classList.remove('waiting');
          answer.textContent = reply;
          scrollChat();
        } else if (update.type === 'tool') {
          reply = '';
          answer.classList.add('waiting');
          answer.textContent = 'BALAJI is handling a tool request…';
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
      answer.remove();
      approval(result.pending_approval);
    } else {
      answer.classList.remove('waiting');
      answer.textContent = result.answer;
      scrollChat();
    }
    setStatus('Ready');
    void refresh();
  } catch (error) {
    answer.classList.remove('waiting');
    if (job.controller.signal.aborted) {
      if (!reply) answer.remove();
      setStatus('Ready', 'Generation stopped.');
    } else {
      if (!reply) answer.textContent = 'Could not complete the reply.';
      setStatus('Error', error.message);
    }
  } finally {
    if (reader) void reader.cancel().catch(() => {});
    chat = null;
    el('stop').disabled = false;
    updateControls();
  }
  // Only a completed assistant answer is eligible for speech. Tool and approval events stay in the UI.
  if (job.allowSpeech && result && !result.pending_approval) void speak(result.answer);
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
el('resources-toggle').addEventListener('click', event => {
  const button = event.currentTarget;
  button.setAttribute('aria-expanded', button.getAttribute('aria-expanded') !== 'true');
});
el('newchat').addEventListener('click', () => {
  stopSpeech();
  showTranscript();
  session = 'chat-' + crypto.randomUUID().replaceAll('-', '').slice(0, 20);
  sessionStorage.setItem('balaji_session', session);
  approvalIds.clear();
  el('chat').replaceChildren();
  msg('assistant', 'New conversation started.');
  setStatus('Ready');
});

updateControls();
void loadConversation();
void refresh();
