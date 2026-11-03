"""Machine scan: GPU, memory, disk -> memory budget and recommended models.

Run at install time (python app\\syscheck.py) and from Settings > System > Rerun scan.
The result is stored in data\\syscheck.json.
"""
import json, os, platform, shutil, subprocess, sys, time

import config

FILE = os.path.join(config.DATA, "syscheck.json")
NOWIN = config.NOWIN

# effective memory bandwidth (GB/s) of common GPUs: this is what sets an LLM's speed
BANDWIDTH = [("5090", 1500), ("5080", 800), ("5070 ti", 750), ("5070", 560), ("5060 ti", 370), ("5060", 370),
             ("4090", 850), ("4080", 600), ("4070 ti", 420), ("4070", 420), ("4060 ti", 240), ("4060", 230),
             ("3090", 780), ("3080", 640), ("3070", 380), ("3060 ti", 380), ("3060", 300), ("2080", 380),
             ("2070", 380), ("2060", 280), ("a6000", 640), ("a5000", 640), ("a4000", 380),
             ("spark", 230), ("gb10", 230)]
UNIFIED_HINTS = ("spark", "gb10")


def _run(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=NOWIN).stdout
    except Exception:
        return ""


def _ps(expr):
    return _run(["powershell", "-NoProfile", "-Command", expr]).strip() if os.name == "nt" else ""


def detect():
    import psutil
    ram = psutil.virtual_memory().total / 2**30
    cpu = _ps("(Get-CimInstance Win32_Processor | Select -First 1).Name") or platform.processor() or "?"
    gpus = []
    out = _run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"])
    for line in out.splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) >= 3 and p[1].replace(".", "").isdigit():
            gpus.append({"name": p[0], "vram_gb": round(float(p[1]) / 1024, 1), "driver": p[2], "vendor": "nvidia"})
    if not gpus and os.name == "nt":  # AMD / Intel: detected but not accelerated by the default install
        for n in filter(None, _ps("(Get-CimInstance Win32_VideoController).Name").splitlines()):
            if "nvidia" not in n.lower() and "basic" not in n.lower():
                gpus.append({"name": n.strip(), "vram_gb": 0, "driver": "", "vendor": "other"})
    os.makedirs(config.LLM_DIR, exist_ok=True)
    disk = shutil.disk_usage(config.LLM_DIR).free / 2**30
    return {"os": f"{platform.system()} {platform.release()}", "arch": platform.machine(), "cpu": cpu.strip(),
            "cores": psutil.cpu_count(logical=False) or psutil.cpu_count(), "ram_gb": round(ram, 1),
            "gpus": gpus, "disk_free_gb": round(disk, 1)}


def assess(hw):
    """Memory budget for models and estimated speed."""
    g = next((x for x in hw["gpus"] if x["vendor"] == "nvidia"), None)
    name = (g or {}).get("name", "").lower()
    unified = bool(g) and (any(h in name for h in UNIFIED_HINTS) or abs(g["vram_gb"] - hw["ram_gb"]) < 0.12 * hw["ram_gb"])
    if g and unified:
        mode, mem = "unified", g["vram_gb"] - 2.0      # memory shared by CPU + GPU, keep enough to run Windows
    elif g:
        mode, mem = "gpu", g["vram_gb"] - 1.0
    else:
        mode, mem = "cpu", max(0.0, hw["ram_gb"] - 8.0)
    bw = next((b for k, b in BANDWIDTH if k in name), None) if g else None
    if bw is None:
        bw = (max(150, g["vram_gb"] * 25) if g else 40)
    image_gb = 7.5                                     # FLUX.2 Klein 4B (or SDXL) during a generation
    if mode == "cpu" or mem < 6:
        tier, label = 1, "Limitée"
    elif mem < 12:
        tier, label = 2, "Correcte"
    elif mem < 22:
        tier, label = 3, "Bonne"
    else:
        tier, label = 4, "Excellente"
    return {"mode": mode, "gpu": g["name"] if g else None, "model_mem_gb": round(mem, 1), "bandwidth": bw,
            "image_gb": image_gb, "tier": tier, "tier_label": label, "images": mode != "cpu" and mem >= 5.5}


def recommend(sysinfo):
    """Best assistant and best image model for this machine (catalog ids)."""
    import catalog
    out, notes = {}, []
    llms = [m for m in catalog.CATALOG if m["kind"] == "llm" and not m.get("custom")]
    ind = {m["id"]: catalog.indicators(m, sysinfo=sysinfo) for m in llms}
    for need_fit, need_speed in ((2, 2), (1, 2), (2, 1), (1, 1)):
        ok = [m for m in llms if ind[m["id"]]["fit"] >= need_fit and ind[m["id"]]["speed"] >= need_speed]
        if ok:  # quality first, with a bonus for fast models (benchmark: MoE models dominate speed/quality)
            best = max(ok, key=lambda m: (m["quality"] + (0.6 if ind[m["id"]]["speed"] == 3 else 0),
                                          m.get("vision", False), ind[m["id"]]["tps"]))
            out["llm"] = best["id"]
            break
    a = sysinfo["assess"]
    if a["images"]:
        # FLUX.2 Klein 4B: ~3 s per image, 7 GB; Qwen-Image (nicer, 10x slower) stays in the library
        imgs = [m for m in catalog.CATALOG if m["kind"] == "image" and not m.get("custom")]
        fits = [m for m in imgs if catalog.indicators(m, sysinfo=sysinfo)["fit"] >= 1]
        pick = next((m for m in fits if m.get("engine") == "klein"), None) or (max(fits, key=lambda m: m["quality"]) if fits else None)
        out["image"] = pick["id"] if pick else None
    else:
        notes.append("Pas de GPU NVIDIA compatible : la génération d'images serait très lente, elle n'est pas recommandée.")
    out["voice"] = "whisper-turbo" if a["mode"] != "cpu" else "whisper-base"  # voice dictation
    if a["mode"] == "cpu":
        notes.append("Sans GPU NVIDIA, l'assistant tourne sur le processeur : choisis un petit modèle.")
    need = sum(catalog.BY_ID[i]["size_gb"] for i in out.values() if i)
    if sysinfo["hw"]["disk_free_gb"] < need + 10:
        notes.append(f"Espace disque faible : {sysinfo['hw']['disk_free_gb']:.0f} Go libres pour ~{need:.0f} Go de modèles recommandés.")
    return out, notes


def run():
    hw = detect()
    info = {"ts": time.time(), "hw": hw, "assess": assess(hw)}
    info["recommended"], info["notes"] = recommend(info)
    config.write_json(FILE, info)
    _cache["v"] = info
    return info


_cache = {"v": None}


def current():
    """Last scan (without rerunning), or a fresh scan if there has never been one."""
    if _cache["v"] is None:
        _cache["v"] = config.read_json(FILE)
        if not isinstance(_cache["v"], dict) or "recommended" not in _cache["v"]:
            return run()
    return _cache["v"]


TIER_EN = {"Limitée": "Limited", "Correcte": "Fair", "Bonne": "Good", "Excellente": "Excellent"}
NOTES_EN = [(r"^Pas de GPU NVIDIA compatible.*", "No compatible NVIDIA GPU: image generation would be very slow, so it isn't recommended."),
            (r"^Sans GPU NVIDIA.*", "Without an NVIDIA GPU the assistant runs on the processor: pick a small model."),
            (r"^Espace disque faible : (\d+) Go libres pour ~(\d+) Go.*", r"Low disk space: \1 GB free for ~\2 GB of recommended models.")]


def note_en(n):
    import re
    for pat, rep in NOTES_EN:
        if re.match(pat, n):
            return re.sub(pat, rep, n)
    return n


def report(info, lang="en"):
    hw, a = info["hw"], info["assess"]
    import catalog
    fr = lang == "fr"
    gb = "Go" if fr else "GB"
    L = (lambda f, e: f if fr else e)
    lines = ["", "  Sparky - " + L("analyse du systeme", "system scan"), "  " + "-" * 40,
             f"  {L('Systeme', 'System'):10}: {hw['os']} ({hw['arch']})",
             f"  {L('Processeur', 'Processor'):10}: {hw['cpu']} ({hw['cores']} {L('coeurs', 'cores')})",
             f"  {L('Memoire', 'Memory'):10}: {hw['ram_gb']:.0f} {gb}",
             f"  {'GPU':10}: " + (", ".join(f"{g['name']} ({g['vram_gb']:.0f} {gb})" if g['vram_gb'] else g['name'] for g in hw["gpus"]) or L("aucun detecte", "none detected")),
             f"  {L('Disque', 'Disk'):10}: {hw['disk_free_gb']:.0f} {gb} {L('libres', 'free')}",
             f"  {L('Niveau', 'Level'):10}: {a['tier_label'] if fr else TIER_EN.get(a['tier_label'], a['tier_label'])} - "
             f"{a['model_mem_gb']:.0f} {gb} {L('utilisables pour les modeles', 'usable for models')}"
             + (L(" (memoire unifiee)", " (unified memory)") if a["mode"] == "unified" else ""), "", "  " + L("Recommande :", "Recommended:")]
    for k, lab in (("llm", "Assistant"), ("image", "Images"), ("voice", L("Voix", "Voice"))):
        mid = info["recommended"].get(k)
        m = catalog.BY_ID.get(mid) if mid else None
        lines.append(f"    {lab:10}: " + (f"{m['label']} ({m['size_gb']:.1f} {gb})" if m else "-"))
    lines += ["  ! " + (n if fr else note_en(n)) for n in info["notes"]] + [""]
    return "\n".join(lines)


if __name__ == "__main__":
    info = run()
    if "--json" in sys.argv:  # for install.ps1: display-ready summary, in the requested language
        import catalog
        fr = "--lang=fr" in sys.argv
        rec = {k: {"id": v, "label": catalog.BY_ID[v]["label"], "size_gb": catalog.BY_ID[v]["size_gb"]}
               for k, v in info["recommended"].items() if v}
        print(json.dumps({"hw": info["hw"], "assess": info["assess"] | {"tier_label": info["assess"]["tier_label"] if fr else TIER_EN.get(info["assess"]["tier_label"])},
                          "recommended": rec, "notes": info["notes"] if fr else [note_en(n) for n in info["notes"]]}))
    else:
        print(report(info, "fr" if "--lang=fr" in sys.argv else "en"))
