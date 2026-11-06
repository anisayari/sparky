// Start only from the companion's Call button. No microphone or device is opened at page load.
let voiceCall = null;

function speechGate(rate, onStart, onFinish) {
  let lead = [], chunks = null, voiced = 0, quiet = 0, samples = 0;
  return function accept(frame, playing) {
    let energy = 0;
    for (const sample of frame) energy += sample * sample;
    const loud = Math.sqrt(energy / frame.length) > (playing ? 0.045 : 0.018);
    if (!chunks) {
      lead.push(new Float32Array(frame));
      while (lead.length > Math.ceil(rate * 0.24 / frame.length)) lead.shift();
      voiced = loud ? voiced + frame.length : 0;
      if (voiced < rate * 0.09) return;
      chunks = lead;
      lead = [];
      samples = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
      quiet = 0;
      onStart();
      return;
    }
    chunks.push(new Float32Array(frame));
    samples += frame.length;
    quiet = loud ? 0 : quiet + frame.length;
    if (quiet < rate * 0.7 && samples < rate * 45) return;
    const complete = chunks;
    chunks = null;
    voiced = 0;
    onFinish(complete);
  };
}

function voiceStatus(message) {
  $('voiceInfo').textContent = tr(message || '');
}

function stopVoicePlayback(call) {
  const player = call.player;
  call.player = null;
  if (player) {
    player.onended = null;
    try { player.stop(); } catch {}
    player.disconnect();
  }
}

function stopVoiceCall(message = '') {
  const call = voiceCall;
  if (!call) return;
  voiceCall = null;
  call.controller.abort();
  stopVoicePlayback(call);
  if (call.processor) {
    call.processor.port.onmessage = null;
    call.processor.port.close();
    call.processor.disconnect();
  }
  call.source?.disconnect();
  call.silent?.disconnect();
  call.stream?.getTracks().forEach(track => { track.onended = null; track.stop(); });
  if (call.ctx?.state !== 'closed') call.ctx?.close().catch(() => {});
  call.stream = call.source = call.processor = call.silent = call.ctx = null;
  $('bmsg').disabled = false;
  $('bsend').disabled = bBusy;
  $('voiceCall').textContent = tr('Appeler');
  $('voiceCall').title = tr('Démarrer un appel vocal local');
  $('voiceCall').setAttribute('aria-pressed', 'false');
  voiceStatus(message);
}

async function toggleVoiceCall() {
  if (voiceCall) { stopVoiceCall(); return; }
  if (bBusy) { voiceStatus('Attendez la fin de la réponse en cours.'); return; }
  const call = {controller: new AbortController(), phase: 'setup'};
  voiceCall = call;
  $('voiceCall').textContent = tr('Raccrocher');
  $('voiceCall').title = tr('Raccrocher');
  $('voiceCall').setAttribute('aria-pressed', 'true');
  $('bmsg').disabled = true;
  $('bsend').disabled = true;
  voiceStatus('Préparation du micro…');
  try {
    call.ctx = new AudioContext();
    await call.ctx.resume();
    const {signal} = call.controller;
    const [voice, piper, bot] = await Promise.all(['/api/voice', '/api/tts', '/api/bot'].map(async url => {
      const response = await fetch(url, {signal});
      if (!response.ok) throw new Error(response.statusText);
      return response.json();
    }));
    if (voiceCall !== call) return;
    if (!voice.localModel) throw new Error('Sélectionnez un modèle local pour l’appel vocal.');
    if (!voice.runtime) throw new Error('Relancez l’installation complète de Sparky pour activer la dictée Whisper.');
    if (!bot.profile.local_only) {
      if (!confirm(tr('Activer le mode local du compagnon pour cet appel et les suivants ?'))) {
        stopVoiceCall(); return;
      }
      const r = await post('/api/bot/profile', {local_only: true});
      if (!r.ok) throw new Error((await r.json()).error || r.statusText);
      if (voiceCall !== call) return;
      if (BOT) BOT.profile.local_only = true;
    }
    if (!voice.ready) {
      await voiceReady();
      if (voiceCall === call) stopVoiceCall();
      return;
    }
    if (!piper.installed) throw new Error('Installez la lecture vocale dans les préférences de l’espace de travail.');
    const voices = piper.voices.filter(v => v.lang === LANG);
    const selected = voices.find(v => v.ready) || voices[0] || piper.voices.find(v => v.ready);
    if (!selected) throw new Error('Installez la voix dans les préférences de l’espace de travail.');
    if (!selected.ready) {
      if (!confirm(tr('Télécharger cette voix pour les appels hors ligne ?') + ' ' + selected.label)) {
        stopVoiceCall(); return;
      }
      const r = await post('/api/tts/install', {voice: selected.id});
      if (!r.ok) throw new Error(r.statusText);
      if (voiceCall === call) stopVoiceCall('La voix se télécharge. Relancez l’appel quand elle sera prête.');
      return;
    }
    call.voice = selected.id;
    await call.ctx.audioWorklet.addModule('/ui/mic-capture.js');
    if (voiceCall !== call) return;
    const stream = await navigator.mediaDevices.getUserMedia({audio: {
      channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true
    }});
    if (voiceCall !== call) { stream.getTracks().forEach(track => track.stop()); return; }
    call.stream = stream;
    for (const track of stream.getAudioTracks()) {
      track.onended = () => { if (voiceCall === call) stopVoiceCall(); };
    }
    call.source = call.ctx.createMediaStreamSource(stream);
    call.processor = new AudioWorkletNode(call.ctx, 'sparky-mic');
    call.silent = call.ctx.createGain();
    call.silent.gain.value = 0;
    call.source.connect(call.processor);
    call.processor.connect(call.silent);
    call.silent.connect(call.ctx.destination);
    const gate = speechGate(call.ctx.sampleRate, () => {
      stopVoicePlayback(call);
      call.phase = 'recording';
      voiceStatus('Parole détectée…');
    }, chunks => voiceExchange(call, chunks));
    call.processor.port.onmessage = event => {
      if (call.phase === 'listening' || call.phase === 'recording' || call.phase === 'speaking')
        gate(event.data, call.phase === 'speaking');
    };
    call.phase = 'listening';
    voiceStatus('À ton écoute…');
  } catch (error) {
    if (voiceCall === call) stopVoiceCall(error.message);
  }
}

async function voiceExchange(call, chunks) {
  call.phase = 'processing';
  voiceStatus('Transcription locale…');
  try {
    const {signal} = call.controller;
    const transcript = await fetch('/api/transcribe', {method: 'POST', signal,
      headers: {'Content-Type': 'audio/wav'}, body: wav16k(chunks, call.ctx.sampleRate)});
    const data = await transcript.json();
    if (!transcript.ok) throw new Error(data.error || transcript.statusText);
    if (voiceCall !== call) return;
    const text = (data.text || '').trim();
    if (text) {
      voiceStatus('Le compagnon répond…');
      const reply = await botStream('/api/bot/voice', {text}, text, signal);
      if (voiceCall !== call) return;
      if (reply) {
        const sound = await fetch('/api/tts', {method: 'POST', signal, headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({text: reply, voice: call.voice, download: false})});
        if (!sound.ok) throw new Error((await sound.json()).error || sound.statusText);
        const buffer = await call.ctx.decodeAudioData(await sound.arrayBuffer());
        if (voiceCall !== call) return;
        const player = call.ctx.createBufferSource();
        player.buffer = buffer;
        player.connect(call.ctx.destination);
        call.player = player;
        player.onended = () => {
          player.disconnect();
          if (voiceCall === call && call.player === player) {
            call.player = null;
            call.phase = 'listening';
            voiceStatus('À ton écoute…');
          }
        };
        call.phase = 'speaking';
        player.start();
        voiceStatus('Lecture vocale… Parle pour interrompre.');
        return;
      }
    }
    call.phase = 'listening';
    voiceStatus('À ton écoute…');
  } catch (error) {
    if (voiceCall === call) stopVoiceCall(error.message);
  }
}

window.addEventListener('pagehide', () => stopVoiceCall());
document.addEventListener('visibilitychange', () => { if (document.hidden) stopVoiceCall(); });
