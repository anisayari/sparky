import shutil
import subprocess
from pathlib import Path

import pytest

import companion
import harness
import memory
import server
from test_server import base as base, call


def test_companion_status_is_offline_and_chat_only_memory_stays_searchable(base, monkeypatch, tmp_path):
    store = memory.Store(str(tmp_path / "bot.db"))
    monkeypatch.setattr(companion, "S", store)
    monkeypatch.setattr(server.catalog, "install", lambda *a: pytest.fail("unexpected model download"))
    monkeypatch.setattr(memory.EMB, "has_runtime", lambda: False)
    monkeypatch.setattr(server.catalog, "installed", lambda model: True)  # weights left from a complete installation
    code, state = call(base, "/api/bot")
    assert code == 200 and not state["embeddings"] and not state["embedRuntime"]
    mid = store.add_memory("Voyage à Strasbourg")
    assert store.search("Strasbourg")[0]["id"] == mid and store.reindex() == 0
    monkeypatch.setattr(server.catalog, "installed", lambda model: False)
    assert call(base, "/api/bot")[0] == 200


def test_voice_request_requires_local_model_and_consent(base, monkeypatch):
    mid = "local-fixture"
    monkeypatch.setitem(server.LLMS, mid, {"id": mid})
    monkeypatch.setitem(server.llm_state, "name", mid)
    monkeypatch.setitem(harness.SETTINGS, "llm", mid)
    allowed = {"local_only": False}
    monkeypatch.setattr(companion, "profile", lambda: allowed)
    seen = []
    def reply(text, emit, *, voice_local):
        seen.append((text, harness.required_model.get(), voice_local))
        emit({"type": "reply", "text": "bonjour"})
    monkeypatch.setattr(companion, "chat", reply)
    assert call(base, "/api/bot/voice", {"text": "hello"})[0] == 409
    allowed["local_only"] = True
    monkeypatch.setitem(server.LLMS[mid], "cloud", True)
    assert call(base, "/api/bot/voice", {"text": "hello"})[0] == 409 and not seen
    monkeypatch.setitem(server.LLMS[mid], "cloud", False)
    code, stream = call(base, "/api/bot/voice", {"text": "hello"})
    assert code == 200 and b"bonjour" in stream and seen == [("hello", mid, True)]


def test_revoked_voice_consent_rejects_before_storing(monkeypatch):
    monkeypatch.setattr(companion, "profile", lambda: {"local_only": False})
    monkeypatch.setattr(companion.S, "add_message", lambda *a: pytest.fail("voice stored after revocation"))
    with pytest.raises(ValueError, match="mode local"):
        companion.chat("private", lambda e: None, voice_local=True)


def test_two_voice_turns_interruption_and_device_release():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    source = (Path(__file__).resolve().parents[1] / "app/ui/voice-call.js").read_text(encoding="utf-8")
    js = """
const assert=require('node:assert/strict');
const elems={}, $=id=>elems[id]||=( {setAttribute(){}} ), tr=s=>s, LANG='fr';
const window={addEventListener(){}},document={addEventListener(){}};
let micStops=0,audioStops=0,ctxCloses=0,calls=0,deviceRequests=0,permission,delayed=false,localModel=true;
const bBusy=false, BOT={profile:{local_only:true}};
const response=v=>({ok:true,json:async()=>v,arrayBuffer:async()=>new ArrayBuffer(4)});
async function fetch(path){const result={'/api/voice':{localModel,ready:true,runtime:true},'/api/tts':{installed:true,
  voices:[{id:'fr',lang:'fr',ready:true}]},'/api/bot':BOT,'/api/transcribe':{text:'bonjour'}};return response(result[path])}
const inputTrack={stop(){micStops++}},input={getTracks:()=>[inputTrack],getAudioTracks:()=>[inputTrack]};
Object.defineProperty(globalThis,'navigator',{value:{mediaDevices:{getUserMedia(){deviceRequests++;
  return delayed?new Promise(ok=>{permission=ok}):Promise.resolve(input)}}},configurable:true});
const port=()=>({connect(){},disconnect(){}});
class AudioWorkletNode { constructor(){this.port={close(){}}} connect(){} disconnect(){} }
class AudioContext {
  constructor(){this.sampleRate=16000;this.destination={};this.state='running';this.audioWorklet={async addModule(){}}}
  async resume(){} async close(){ctxCloses++;this.state='closed'}
  createMediaStreamSource(){return port()}
  createGain(){return {...port(),gain:{}}} async decodeAudioData(){return {}}
  createBufferSource(){return {...port(),start(){},stop(){audioStops++}}}
}
const wav16k=()=>new Uint8Array(4);
async function botStream(path,body,text){assert.equal(path,'/api/bot/voice');assert.equal(text,'bonjour');calls++;return 'salut'}
const settle=()=>new Promise(ok=>setImmediate(ok));
""" + source + """
(async()=>{
  localModel=false;await toggleVoiceCall();assert.equal(deviceRequests,0);assert.equal(voiceCall,null);
  localModel=true;await toggleVoiceCall();const call=voiceCall;assert.equal(call.phase,'listening');
  const frame=amplitude=>call.processor.port.onmessage({data:new Float32Array(1024).fill(amplitude)});
  const frames=(n,value)=>{for(let i=0;i<n;i++)frame(value)};
  frame(.8);frames(16,0);assert.equal(calls,0); // a click is not speech
  frames(4,.13);frames(15,0);await settle();assert.equal(call.phase,'speaking');assert.equal(calls,1);
  frames(3,.16);assert.equal(call.phase,'recording');assert.equal(audioStops,1);
  frames(15,0);await settle();assert.equal(calls,2);assert.equal(call.phase,'speaking');
  const port=call.processor.port;
  stopVoiceCall();assert.equal(audioStops,2);assert.equal(micStops,1);assert.equal(port.onmessage,null);
  assert.equal(call.processor,null);assert.equal(call.stream,null);assert.equal(call.ctx,null);
  delayed=true;const opening=toggleVoiceCall();await settle();stopVoiceCall();permission(input);
  await opening;assert.equal(micStops,2);assert.equal(ctxCloses,3);assert.equal(voiceCall,null);
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    subprocess.run([node, "-e", js], check=True, timeout=20)
