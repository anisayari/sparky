"""Repository hygiene: things that broke installs before."""
import os, re, shutil, subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(p, mode="r"):
    with open(os.path.join(ROOT, p), mode, **({} if "b" in mode else {"encoding": "utf-8"})) as f:
        return f.read()


@pytest.mark.parametrize("script", ["install.ps1", "start.ps1", "stop.ps1", "sync-zip.ps1"])
def test_powershell_scripts_are_ascii_without_bom(script):
    """A BOM or a non-ASCII byte breaks the one-line install `irm ... | iex`."""
    data = read(script, "rb")
    assert not data.startswith(b"\xef\xbb\xbf")
    assert all(b < 128 for b in data), f"non-ASCII byte in {script}: write accents as \\uXXXX"


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+\n?", read("VERSION"))


def test_requirements_cover_the_imports():
    req = read("requirements.txt").lower()
    for pkg in ("psutil", "pillow", "pypdf", "anthropic"):
        assert pkg in req


def test_installer_never_downloads_models_by_default():
    ps = read("install.ps1")
    assert "if ($Models -and -not $NoModels" in ps


def test_page_script_parses():
    node = shutil.which("node")
    if not node:
        pytest.skip("node missing")
    html = read("app/index.html")
    scripts = re.findall(r"<script>([\s\S]*?)</script>", html)
    assert scripts
    for js in scripts:
        r = subprocess.run([node, "-e", "new Function(require('fs').readFileSync(0, 'utf8'))"], input=js,
                           capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, r.stderr
    directory = os.path.join(ROOT, "app", "ui")
    for name in os.listdir(directory):
        if name.endswith(".js"):
            subprocess.run([node, "--check", os.path.join(directory, name)], check=True, capture_output=True)


def test_every_api_route_used_by_the_page_exists():
    html, server = read("app/index.html"), read("app/server.py")
    used = set(re.findall(r"""fetch\(\s*['"](/api/[a-z/]+)""", html))
    missing = [u for u in used if f'"{u}"' not in server and f'"{u.rstrip("/")}' not in server]
    assert not missing, missing


def test_generated_projects_load_from_the_preview_origin_only():
    """Generated code must never run on the API's origin (it could drive Sparky)."""
    html = read("app/index.html")
    uses = re.findall(r".{0,12}/api/proj/", html)
    assert uses and all("PREVIEW" in u for u in uses), uses
    assert '"/api/proj/"' not in read("app/server.py").split("class PreviewHandler")[0]
