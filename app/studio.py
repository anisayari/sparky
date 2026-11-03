"""Studio: a media library of everything generated (images and videos, from the Studio and from chats) and a
Grok-Imagine-like prompt bar: text -> image, text -> video, image -> video.

Video engines (ComfyUI):
- "ltx": LTX-2.5 22B distilled, video + synchronized sound, 8 steps (Lightricks).
- "wan": Wan 2.2 TI2V 5B, lighter, no sound (one model for text and image to video).
Jobs run one at a time; a big video model does not fit next to a local assistant, so the server pauses the
assistant during a video (hooks set by server.py) and restarts it afterwards.
"""
import base64, io, json, os, random, re, shutil, threading, time, uuid

from PIL import Image

import config
import harness
import turn_runtime

DIR = os.path.join(config.DATA, "studio")
INDEX = os.path.join(DIR, "index.json")
ASPECTS = {"16:9": (16, 9), "9:16": (9, 16), "1:1": (1, 1), "4:3": (4, 3), "3:4": (3, 4)}
IMAGE_SIZES = {"16:9": (1216, 688), "9:16": (688, 1216), "1:1": (1024, 1024), "4:3": (1152, 864), "3:4": (864, 1152)}
VIDEO_NEGATIVE = "blurry, low quality, distorted, deformed, watermark, text, subtitles, static, frozen"
MAX_SECONDS = 10

jobs = []          # [{id, mode, prompt, ..., state: queued|running|done|error, started, est, result, error}]
_lock = threading.Lock()
_wake = threading.Event()
_thread = None
_running = {}
hooks = {"before_video": lambda m: None, "after_queue": lambda: None}  # set by server.py (pause / resume the assistant)


# ---------------------------------------------------------------- library
def _load():
    return config.read_json(INDEX, []) or []


def _save(items):
    config.write_json(INDEX, items)


def _add(item):
    with _lock:
        items = _load()
        items.insert(0, item)
        _save(items)
    return item


_chat_cache = {"key": None, "items": []}


def _chat_media():
    """Generated images of every saved conversation (read from their session.json, cached on modification times)."""
    rows = []
    for d in os.listdir(config.DATA) if os.path.isdir(config.DATA) else []:
        p = os.path.join(config.DATA, d, "session.json")
        if re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", d) and os.path.exists(p):
            rows.append((d, os.path.getmtime(p)))
    key = tuple(sorted(rows))
    if key == _chat_cache["key"]:
        return _chat_cache["items"]
    out = []
    for sid, _ in rows:
        try:
            st = json.load(open(os.path.join(config.DATA, sid, "session.json"), encoding="utf-8"))
        except (OSError, ValueError):
            continue
        title = (st.get("meta") or {}).get("title") or st.get("title") or ""
        for n, v in (st.get("images") or {}).items():
            if v.get("kind") == "gen" and os.path.exists(v.get("path", "")):
                out.append({"id": f"chat-{sid}-{n}", "type": "image", "url": f"/api/img/{sid}/{n}", "prompt": v.get("desc", ""),
                            "created": os.path.getmtime(v["path"]), "source": "chat", "session": sid, "title": title})
    _chat_cache.update(key=key, items=out)
    return out


def library(kind="all"):
    items = [i | {"source": "studio", "url": "/api/studio/file/" + i["file"],
                  "poster": ("/api/studio/file/" + i["poster"]) if i.get("poster") else None} for i in _load()]
    items += _chat_media()
    if kind in ("image", "video"):
        items = [i for i in items if i["type"] == kind]
    return sorted(items, key=lambda i: -i["created"])


def file_path(name):
    if not re.fullmatch(r"[\w-]+\.(png|jpg|mp4|webm)", name or ""):
        return None
    p = os.path.join(DIR, name)
    return p if os.path.exists(p) else None


def delete(item_id):
    """Removes a Studio item (its files go to the data folder's trash, not erased)."""
    with _lock:
        items = _load()
        it = next((i for i in items if i["id"] == item_id), None)
        if not it:
            return False
        trash = os.path.join(DIR, "trash")
        os.makedirs(trash, exist_ok=True)
        for k in ("file", "poster"):
            if it.get(k) and os.path.exists(os.path.join(DIR, it[k])):
                shutil.move(os.path.join(DIR, it[k]), os.path.join(trash, it[k]))
        _save([i for i in items if i["id"] != item_id])
    return True


def source_image(ref):
    """PIL image of a library item (studio id, 'chat-<sid>-<n>') or a data URL."""
    if not ref:
        return None
    if str(ref).startswith("data:"):
        return Image.open(io.BytesIO(base64.b64decode(ref.split(",", 1)[1]))).convert("RGB")
    m = re.fullmatch(r"chat-(\d{8}-\d{6}-[0-9a-f]{4})-(\d+)", str(ref))
    if m:
        st = json.load(open(os.path.join(config.DATA, m[1], "session.json"), encoding="utf-8"))
        return Image.open(st["images"][m[2]]["path"]).convert("RGB")
    it = next((i for i in _load() if i["id"] == ref), None)
    if not it:
        raise ValueError("image inconnue")
    p = os.path.join(DIR, it["poster"] if it["type"] == "video" else it["file"])
    return Image.open(p).convert("RGB")


# ---------------------------------------------------------------- video models
def video_model():
    """Catalog entry of the chosen video model (setting "video"), else the first installed one."""
    import catalog
    vids = [m for m in catalog.CATALOG if m["kind"] == "video"]
    m = catalog.BY_ID.get(harness.SETTINGS.get("video"))
    if m and m["kind"] == "video" and catalog.installed(m):
        return m
    return next((v for v in vids if catalog.installed(v)), None)


def _dims(aspect, long_side, mult):
    a, b = ASPECTS.get(aspect, ASPECTS["16:9"])
    if a >= b:
        w, h = long_side, long_side * b / a
    else:
        w, h = long_side * a / b, long_side
    return int(round(w / mult) * mult), int(round(h / mult) * mult)


def build_ltx(m, prompt, seed, aspect="16:9", seconds=5, image=None, fps=24):
    """LTX-2.5 distilled, one stage: video + sound latents sampled together (8 steps), optional first frame."""
    w, h = _dims(aspect, m.get("long_side", 1024), 32)
    frames = int(seconds * fps) // 8 * 8 + 1  # LTX wants 8k + 1 frames
    g = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": m["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": m["clip"], "type": "ltxv", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": m["audio_vae"]}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": m["vae"]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": VIDEO_NEGATIVE, "clip": ["2", 0]}},
        "7": {"class_type": "LTXVConditioning", "inputs": {"positive": ["5", 0], "negative": ["6", 0], "frame_rate": fps}},
        "8": {"class_type": "EmptyLTXVLatentVideo", "inputs": {"width": w, "height": h, "length": frames, "batch_size": 1}},
        "9": {"class_type": "LTXVEmptyLatentAudio", "inputs": {"audio_vae": ["3", 0], "frames_number": frames,
                                                               "frame_rate": fps, "batch_size": 1}},
        "11": {"class_type": "CFGGuider", "inputs": {"model": ["1", 0], "positive": ["7", 0], "negative": ["7", 1], "cfg": 1}},
        "12": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler_ancestral" if image else "euler"}},
        "13": {"class_type": "ManualSigmas", "inputs": {"sigmas": "1.0, 0.99375, 0.9875, 0.98125, 0.975, 0.909375, 0.725, 0.421875, 0.0"}},
        "14": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "15": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["14", 0], "guider": ["11", 0], "sampler": ["12", 0],
                                                                 "sigmas": ["13", 0], "latent_image": ["10", 0]}},
        "16": {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["15", 0]}},
        "17": {"class_type": "VAEDecodeTiled", "inputs": {"samples": ["16", 0], "vae": ["4", 0], "tile_size": 512, "overlap": 64,
                                                          "temporal_size": 64, "temporal_overlap": 16}},
        "18": {"class_type": "LTXVAudioVAEDecode", "inputs": {"samples": ["16", 1], "audio_vae": ["3", 0]}},
        "19": {"class_type": "CreateVideo", "inputs": {"images": ["17", 0], "audio": ["18", 0], "fps": fps}},
        "20": {"class_type": "SaveVideo", "inputs": {"video": ["19", 0], "filename_prefix": "sparky_studio", "format": "mp4", "codec": "auto"}},
    }
    video_latent = ["8", 0]
    if image:  # first frame: the picture, then the motion
        g["30"] = {"class_type": "LoadImage", "inputs": {"image": image}}
        g["31"] = {"class_type": "ImageScale", "inputs": {"image": ["30", 0], "upscale_method": "lanczos", "width": w, "height": h, "crop": "center"}}
        g["32"] = {"class_type": "LTXVPreprocess", "inputs": {"image": ["31", 0], "img_compression": 18}}
        g["33"] = {"class_type": "LTXVImgToVideoInplace", "inputs": {"vae": ["4", 0], "image": ["32", 0], "latent": ["8", 0],
                                                                     "strength": 1.0, "bypass": False}}
        video_latent = ["33", 0]
    g["10"] = {"class_type": "LTXVConcatAVLatent", "inputs": {"video_latent": video_latent, "audio_latent": ["9", 0]}}
    return g, {"width": w, "height": h, "frames": frames, "fps": fps}


def build_wan(m, prompt, seed, aspect="16:9", seconds=5, image=None, fps=24):
    """Wan 2.2 TI2V 5B: text or image to video in the same model (720p class, 24 fps, no sound)."""
    w, h = _dims(aspect, m.get("long_side", 1280), 32)
    frames = int(seconds * fps) // 4 * 4 + 1  # Wan wants 4k + 1 frames
    g = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": m["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": m["clip"], "type": "wan", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": m["vae"]}},
        "4": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": 8.0}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": VIDEO_NEGATIVE, "clip": ["2", 0]}},
        "7": {"class_type": "Wan22ImageToVideoLatent", "inputs": {"vae": ["3", 0], "width": w, "height": h, "length": frames, "batch_size": 1}},
        "8": {"class_type": "KSampler", "inputs": {"model": ["4", 0], "positive": ["5", 0], "negative": ["6", 0], "latent_image": ["7", 0],
                                                   "seed": seed, "steps": 20, "cfg": 5.0, "sampler_name": "uni_pc", "scheduler": "simple", "denoise": 1.0}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "CreateVideo", "inputs": {"images": ["9", 0], "fps": fps}},
        "11": {"class_type": "SaveVideo", "inputs": {"video": ["10", 0], "filename_prefix": "sparky_studio", "format": "mp4", "codec": "auto"}},
    }
    if image:
        g["30"] = {"class_type": "LoadImage", "inputs": {"image": image}}
        g["31"] = {"class_type": "ImageScale", "inputs": {"image": ["30", 0], "upscale_method": "lanczos", "width": w, "height": h, "crop": "center"}}
        g["7"]["inputs"]["start_image"] = ["31", 0]
    return g, {"width": w, "height": h, "frames": frames, "fps": fps}


# ---------------------------------------------------------------- generation
def _comfy_outputs(graph):
    """Runs a graph; returns the paths of its saved files (images or videos)."""
    e = harness.comfy_result(graph, max_seconds=7200)
    files = [f for o in e["outputs"].values() for k in ("images", "videos", "gifs") for f in o.get(k, [])]
    return [os.path.join(harness.COMFY_OUT, f.get("subfolder", ""), f["filename"]) for f in files if f.get("type", "output") == "output"]


def enhance(prompt, mode, from_image=False):
    """Rewrites a short idea as a detailed prompt with the active assistant (skipped when it is unavailable)."""
    if mode == "video" and from_image:
        ask = ("The video starts from an existing picture that you cannot see. Rewrite the user's idea as one English prompt "
               "for an image-to-video generator: describe ONLY what moves and how (subjects, elements, speed), the camera "
               "movement and the sounds (ambient sound, voices, music). Do not invent the setting, the time of day or new "
               "objects. Present tense, max 70 words. Answer with the prompt only.")
    elif mode == "image":
        ask = ("Rewrite this idea as one detailed English prompt for an image generator (subject, setting, light, framing, "
               "style), max 70 words. Answer with the prompt only.")
    else:
        ask = ("Rewrite this idea as one detailed English prompt for a video generator: the subject, the action and the motion "
               "over time, the camera movement, the light and the atmosphere, and the sounds (ambient sound, voices, music) "
               "since the model also makes audio. Present tense, max 90 words. Answer with the prompt only.")
    try:
        out = harness.chat_once({"messages": [{"role": "system", "content": ask}, {"role": "user", "content": prompt}],
                                 "max_tokens": 300, "temperature": 0.7, "chat_template_kwargs": {"enable_thinking": False}})
        out = re.sub(r"<think>.*?</think>", "", out or "", flags=re.S).strip()
        out = re.sub(r"^(\*\*)?(enhanced |detailed |video |image )?prompt(\*\*)?\s*:\s*", "", out, flags=re.I).strip().strip('"')
        return out if 15 < len(out) < 1500 else prompt
    except Exception:
        return prompt


def _poster(video, dest):
    """First frame of a video as a PNG (thumbnail), with PyAV (shipped with ComfyUI)."""
    try:
        import av
        with av.open(video) as c:
            for frame in c.decode(video=0):
                frame.to_image().save(dest)
                return True
    except Exception as e:
        print("poster:", e)
    return False


def est_seconds(job):
    """Rough duration estimate (measured speeds when available)."""
    sp = harness.SETTINGS.get("speed", {})
    if job["mode"] == "image":
        return sp.get("img:" + (harness.SETTINGS.get("ckpt") or ""), 15)
    m = video_model() or {}
    per_sec = sp.get("vid:" + m.get("id", ""), m.get("est_secs_per_s", 20))
    return 20 + per_sec * job["seconds"]


def _enhance_job(j):
    """Detailed prompt, made once per job: started at submit time (the assistant may be paused later for a video)."""
    if not (j.get("enhance") and j["prompt"]) or "final_prompt" in j:
        return
    if j.get("enhancing"):  # already being written by the submit thread
        while "final_prompt" not in j and j.get("enhancing"):
            turn_runtime.check()
            time.sleep(0.3)
        return
    j["enhancing"] = True
    try:
        j["final_prompt"] = enhance(j["prompt"], j["mode"], bool(j.get("source")))
    finally:
        j["enhancing"] = False


def _run(job):
    mid = uuid.uuid4().hex[:10]
    os.makedirs(DIR, exist_ok=True)
    seed = random.randrange(2 ** 31)
    job["status"] = "prompt" if job.get("enhance") and "final_prompt" not in job else job["status"]
    if job["mode"] == "video":  # enhance every queued video prompt now: the assistant is paused during the videos
        for j in [job] + [j for j in jobs if j["state"] == "queued" and j["mode"] == "video"]:
            _enhance_job(j)
    _enhance_job(job)
    turn_runtime.check()
    prompt = job.get("final_prompt") or job["prompt"]
    src =source_image(job.get("source")) if job.get("source") else None
    item = {"id": mid, "prompt": job["prompt"], "final_prompt": prompt, "created": time.time(), "aspect": job["aspect"],
            "mode": job["mode"], "seed": seed,
            "parent": job["source"] if job.get("source") and not str(job["source"]).startswith("data:") else None}
    if job["mode"] == "image":
        job["status"] = "image"
        m = harness.image_model()
        size = IMAGE_SIZES.get(job["aspect"], IMAGE_SIZES["1:1"])
        if m.get("engine") in harness.INSTRUCT:
            g = harness.build_instruct(m, prompt, seed, size=size, images=[src] if src else ())
        else:
            g = harness.build_graph(prompt, harness.NEGATIVE, seed, size=size)
        t0 = time.time()
        out = harness.comfy_run(g)
        Image.open(out).convert("RGB").save(os.path.join(DIR, mid + ".png"))
        return _add(item | {"type": "image", "file": mid + ".png", "model": m.get("label"), "secs": round(time.time() - t0)})
    m = video_model()
    if not m:
        raise RuntimeError("aucun modèle vidéo installé : ouvre la Bibliothèque de modèles (filtre Vidéos)")
    job["status"] = "memory"
    hooks["before_video"](m)
    img_name = None
    if src:
        os.makedirs(harness.COMFY_IN, exist_ok=True)
        img_name = f"sparky_studio_{mid}.png"
        src.save(os.path.join(harness.COMFY_IN, img_name))
    build = build_ltx if m["engine"] == "ltx" else build_wan
    g, info = build(m, prompt, seed, job["aspect"], job["seconds"], img_name)
    job["status"] = "video"
    t0 = time.time()
    with harness.busy("image"):
        outs = _comfy_outputs(g)
    secs = time.time() - t0
    vid = next((o for o in outs if o.lower().endswith((".mp4", ".webm"))), None)
    if not vid:
        raise RuntimeError("ComfyUI n'a pas produit de vidéo")
    shutil.copy(vid, os.path.join(DIR, mid + ".mp4"))
    poster = mid + "_poster.png" if _poster(vid, os.path.join(DIR, mid + "_poster.png")) else None
    harness.record_speed("vid:" + m["id"], secs / max(1, job["seconds"]))
    return _add(item | {"type": "video", "file": mid + ".mp4", "poster": poster, "model": m["label"], "secs": round(secs),
                        "seconds": round(info["frames"] / info["fps"], 1), "width": info["width"], "height": info["height"],
                        "audio": m["engine"] == "ltx"})


def _worker():
    while True:
        _wake.wait(5)
        _wake.clear()
        had_video = False
        while True:
            with _lock:
                job = next((j for j in jobs if j["state"] == "queued"), None)
                if job:
                    job.update(state="running", started=time.time())
                    token = _running[job["id"]] = threading.Event()
            if not job:
                break
            job["est"] = est_seconds(job)
            turn_runtime.local.token = token
            had_video = had_video or job["mode"] == "video"
            try:
                job["result"] = _run(job)
                job["state"] = "done"
            except Exception as e:
                job.update(state="error", error=str(e))
                print("studio:", e)
            finally:
                turn_runtime.local.token = None
                with _lock:
                    _running.pop(job["id"], None)
            job["ended"] = time.time()
        if had_video:
            try:
                hooks["after_queue"]()
            except Exception as e:
                print("studio resume:", e)


def submit(mode, prompt, aspect="16:9", seconds=5, source=None, enhance_prompt=True):
    """Queues a generation; returns the job (polled through status())."""
    global _thread
    prompt = (prompt or "").strip()
    if mode not in ("image", "video"):
        raise ValueError("mode inconnu")
    if not prompt and not source:
        raise ValueError("décris ce que tu veux créer")
    if mode == "video" and not prompt:
        prompt = "The scene comes to life with natural, subtle motion and ambient sound."
    job = {"id": uuid.uuid4().hex[:8], "mode": mode, "prompt": prompt[:4000], "aspect": aspect if aspect in ASPECTS else "16:9",
           "seconds": max(2, min(MAX_SECONDS, int(seconds or 5))), "source": source, "enhance": bool(enhance_prompt),
           "state": "queued", "status": "", "created": time.time()}
    with _lock:
        if len(jobs) >= 50:
            old = next((j for j in jobs if j["state"] in ("done", "error")), None)
            if old is None:
                raise ValueError("La file du Studio est pleine.")
            jobs.remove(old)
        jobs.append(job)
        if _thread is None:
            _thread = threading.Thread(target=_worker, daemon=True)
            _thread.start()
    if job["enhance"]:
        threading.Thread(target=_enhance_job, args=(job,), daemon=True).start()
    _wake.set()
    return {k: v for k, v in job.items() if k != "source"} | {"hasSource": bool(source)}


def cancel(job_id):
    with _lock:
        if job_id in _running:
            _running[job_id].set()
            return True
        job = next((j for j in jobs if j["id"] == job_id and j["state"] == "queued"), None)
        if job:
            job.update(state="error", error="Génération arrêtée.", ended=time.time())
        return job is not None


def status():
    now = time.time()
    out = []
    for j in jobs:
        if j["state"] in ("done", "error") and j.get("ended", 0) < now - 600:
            continue
        el = (j.get("ended") or now) - j["started"] if j.get("started") else 0
        out.append({k: v for k, v in j.items() if k not in ("source",)} | {
            "elapsed": round(el), "progress": min(0.97, el / j["est"]) if j.get("est") and j["state"] == "running" else None,
            "sourceThumb": j["source"] if isinstance(j.get("source"), str) and not j["source"].startswith("data:") else None})
    return out
