"""Hardware -> recommended models (regression: what each kind of PC gets by default)."""
import pytest

import syscheck


def machine(gpu=None, vram=0, ram=32, disk=500):
    gpus = [{"name": gpu, "vram_gb": vram, "driver": "600", "vendor": "nvidia"}] if gpu else []
    hw = {"os": "Windows 11", "arch": "AMD64", "cpu": "x", "cores": 8, "ram_gb": ram, "gpus": gpus, "disk_free_gb": disk}
    info = {"hw": hw, "assess": syscheck.assess(hw)}
    info["recommended"], info["notes"] = syscheck.recommend(info)
    return info


def test_rtx_5090_gets_the_smartest_model_and_fast_images():
    i = machine("NVIDIA GeForce RTX 5090", 31.8, 64)
    assert i["assess"]["tier_label"] == "Excellente"
    assert i["recommended"] == {"llm": "qwen38", "image": "klein4b", "voice": "whisper-turbo"} and not i["notes"]


@pytest.mark.parametrize("gpu,vram", [("NVIDIA GeForce RTX 4060", 8), ("NVIDIA GeForce RTX 3060", 12)])
def test_mid_range_gpus_get_a_model_that_fits(gpu, vram):
    import catalog
    i = machine(gpu, vram)
    llm = catalog.BY_ID[i["recommended"]["llm"]]
    assert llm["size_gb"] + 2.5 <= i["assess"]["model_mem_gb"]
    assert i["recommended"]["voice"] == "whisper-turbo"


def test_image_model_is_recommended_only_when_it_fits():
    """FLUX.2 Klein's size depends on the GPU running the tests (FP4 text encoder on RTX 50, FP8 elsewhere)."""
    import catalog
    klein = catalog.BY_ID["klein4b"]["size_gb"]
    for vram in (8, 12, 16):
        i = machine("NVIDIA GeForce RTX 4070", vram)
        fits = klein <= i["assess"]["model_mem_gb"] + 2
        assert (i["recommended"].get("image") is not None) == fits, (vram, klein)


def test_no_gpu_runs_a_small_model_without_images():
    i = machine(None, ram=16)
    assert i["assess"]["mode"] == "cpu" and i["assess"]["tier"] == 1
    assert "image" not in i["recommended"] and i["recommended"]["voice"] == "whisper-base"
    assert any("processeur" in n for n in i["notes"])


def test_low_disk_space_is_reported():
    assert any("Espace disque faible" in n for n in machine("NVIDIA GeForce RTX 5090", 31.8, 64, disk=12)["notes"])


def test_unified_memory_machines_keep_memory_for_windows():
    i = machine("NVIDIA GB10 Spark", 120, 128)
    assert i["assess"]["mode"] == "unified" and i["assess"]["model_mem_gb"] == 118


def mac(chip, ram, gpu_gb=None):
    gpus = [{"name": chip, "vram_gb": gpu_gb or ram * (2 / 3 if ram <= 36 else 3 / 4), "driver": "Metal", "vendor": "apple"}]
    hw = {"os": "Darwin 24.6.0", "arch": "arm64", "cpu": chip, "cores": 12, "ram_gb": ram, "gpus": gpus, "disk_free_gb": 500}
    info = {"hw": hw, "assess": syscheck.assess(hw)}
    info["recommended"], info["notes"] = syscheck.recommend(info)
    return info


def test_apple_silicon_is_unified_memory_on_the_gpu():
    i = mac("Apple M4 Pro", 24)
    a = i["assess"]
    assert a["mode"] == "unified" and a["apple"] and a["gpu"] == "Apple M4 Pro"
    assert a["model_mem_gb"] == 15.0 and a["bandwidth"] == 230  # "m4 pro" matched before "m4"
    assert i["recommended"]["voice"] == "whisper-turbo" and "llm" in i["recommended"]


def test_apple_silicon_gets_no_image_model_yet():
    i = mac("Apple M3 Max", 64)
    assert not i["assess"]["images"] and "image" not in i["recommended"]
    assert any(n.startswith("Mac :") for n in i["notes"]) and not any("NVIDIA" in n for n in i["notes"])
    assert syscheck.note_en(i["notes"][0]).startswith("Mac: image generation")


@pytest.mark.parametrize("chip,bw", [("Apple M1", 58), ("Apple M2 Ultra", 680), ("Apple M4", 100), ("Apple M9", 100)])
def test_apple_bandwidth_by_chip(chip, bw):
    assert mac(chip, 16)["assess"]["bandwidth"] == bw


def test_apple_gpu_share_follows_the_wired_limit(monkeypatch):
    monkeypatch.setattr(syscheck.sys, "platform", "darwin")
    monkeypatch.setattr(syscheck.platform, "machine", lambda: "arm64")
    vals = {"iogpu.wired_limit_mb": "0", "machdep.cpu.brand_string": "Apple M4 Pro"}
    monkeypatch.setattr(syscheck, "_sysctl", lambda k: vals[k])
    assert syscheck.apple_gpu(24)["vram_gb"] == 16.0 and syscheck.apple_gpu(64)["vram_gb"] == 48.0
    vals["iogpu.wired_limit_mb"] = "20480"  # raised by the user (sudo sysctl iogpu.wired_limit_mb=20480)
    assert syscheck.apple_gpu(24)["vram_gb"] == 20.0
    monkeypatch.setattr(syscheck.platform, "machine", lambda: "x86_64")  # Intel Mac: no Metal acceleration here
    assert syscheck.apple_gpu(24) is None
