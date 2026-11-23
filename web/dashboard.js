const el = id => document.getElementById(id);
const prompt = el('prompt');
const speaker = el('speaker');
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
const approvalIds = new Set();
let session = sessionStorage.getItem('balaji_session') || 'web';
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
  el('voice').dataset.state = state.toLowerCase();
  el('orb-state').textContent = state === 'Ready' ? 'Idle' : state;
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
  el('voice-mode').textContent = voiceMode ? 'End voice session' : 'Start voice conversation';
  el('voice-mode').disabled = micPermissionPending && !voiceMode;
  el('voice-settings').hidden = !voiceMode;
  el('mic').hidden = !voiceMode || el('auto-send').checked;
  el('mic').disabled = micPermissionPending || Boolean(chat || capture || voiceApproval);
  el('send').disabled = Boolean(chat || capture || voiceApproval);
  el('newchat').disabled = Boolean(chat || capture || voiceApproval);
  prompt.readOnly = Boolean(capture);
  el('stop').hidden = !(capture || chat || speech);
  el('stop').textContent = capture?.phase === 'recording' ? (capture.automatic ? 'Send now' : 'Stop recording')
    : chat ? 'Stop generation' : speech ? 'Stop playback' : 'Cancel';
  el('play').hidden = !speech?.needsGesture;
  el('retry').hidden = !retryRequest || Boolean(chat || capture || voiceApproval);
}

function endVoiceSession(state = 'Ready', detail = '') {
  voiceMode = false;
  voiceApproval = null;
  if (capture) endCapture(capture);
  if (audioContext && audioContext.state !== 'closed') void audioContext.close().catch(() => {});
  audioContext = null;
  if (chat) chat.allowSpeech = false;
  stopSpeech();
  setStatus(state, detail);
  updateControls();
}

function continueConversation(detail = '') {
  updateControls();
  if (voiceApproval) {
    setStatus('Approval needed', 'Choose Approve or Reject to continue the voice conversation.');
  } else if (voiceMode && el('auto-send').checked && !chat && !capture && !speech && !micPermissionPending) {
    void startRecording(true, detail);
  } else {
    setStatus('Ready', detail || (voiceMode && !el('auto-send').checked ? 'Use the microphone button to record.' : ''));
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

function approval(p) {
  if (approvalIds.has(p.id)) return;
  approvalIds.add(p.id);
  const originSession = session;
  const container = msg('assistant', `Permission required: ${p.tool_name}\n${JSON.stringify(p.preview || p.arguments, null, 2)}`);
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
        if (session === originSession) {
          const message = result.result || result.status;
          msg('assistant', message);
          if (voiceApproval === p.id) {
            voiceApproval = null;
            void speak(message);
          }
        }
        void refresh();
        document.dispatchEvent(new Event('balaji:refresh'));
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

function finishSpeech() {
  stopSpeech();
  continueConversation();
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
    el('meter').style.setProperty('--level', Math.min(1, 0.14 + rms * 4));
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
    if (job.automatic && /^(?:end (?:the )?voice session|stop listening)[.!?]*$/i.test(transcript)) {
      endVoiceSession('Ready', 'Voice session ended.');
      return;
    }
    prompt.value = transcript.slice(0, prompt.maxLength);
    setStatus('Ready', transcript.length > prompt.maxLength ? 'The transcript exceeds the chat limit. Edit it before sending.' : '');
    if (el('auto-send').checked && transcript.length <= prompt.maxLength) el('form').requestSubmit();
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
  if (!voiceMode || chat || capture || voiceApproval || micPermissionPending) return;
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

el('mic').addEventListener('click', () => void startRecording());
el('voice-mode').addEventListener('click', () => {
  if (voiceMode) {
    endVoiceSession('Ready', 'Voice session ended.');
    return;
  }
  voiceMode = true;
  if (!chat) continueConversation();
  else updateControls();
});

el('auto-send').addEventListener('change', () => {
  if (!el('auto-send').checked && capture?.automatic) endCapture(capture);
  if (!chat && !capture && !speech) continueConversation();
  else updateControls();
});

el('stop').addEventListener('click', () => {
  if (chat) {
    chat.controller.abort();
    el('stop').disabled = true;
  } else if (capture?.phase === 'recording') {
    finishRecording(capture);
  } else if (speech) {
    finishSpeech();
  } else {
    endVoiceSession('Ready', 'Voice session ended.');
  }
});

document.addEventListener('visibilitychange', () => {
  if (document.hidden && voiceMode) endVoiceSession('Ready', 'Voice session ended when you left this tab.');
});

async function runChat(text, key) {
  if (chat || capture || voiceApproval || !text) return;
  stopSpeech();
  retryRequest = null;
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
          reply += update.text;
          answer.classList.remove('waiting');
          answer.textContent = reply;
          scrollChat();
        } else if (update.type === 'tool') {
          reply = '';
          answer.classList.add('waiting');
          answer.textContent = 'BALAJI is handling a tool request…';
          setStatus('Executing');
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
      if (voiceMode) voiceApproval = result.pending_approval.id;
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
  stopSpeech();
  showTranscript();
  session = 'chat-' + crypto.randomUUID().replaceAll('-', '').slice(0, 20);
  sessionStorage.setItem('balaji_session', session);
  approvalIds.clear();
  retryRequest = null;
  updateControls();
  el('chat').replaceChildren();
  msg('assistant', 'New conversation started.');
  setStatus('Ready');
});

updateControls();
