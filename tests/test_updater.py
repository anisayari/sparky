"""Version check and update (git) - regression tests for the update banner."""
import os, shutil, subprocess

import pytest

import config
import updater


@pytest.mark.parametrize("a,b", [("1.2.0", "1.1.9"), ("1.10.0", "1.9.9"), ("2.0", "1.99.99"), ("1.2.1", "1.2")])
def test_versions_compare_as_numbers(a, b):
    assert updater._num(a) > updater._num(b)


def git(root, *args):
    subprocess.run(["git", "-C", root, *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    if not shutil.which("git"):
        pytest.skip("git missing")
    root = str(tmp_path / "sparky")
    os.makedirs(os.path.join(root, "app"))
    for f, t in (("VERSION", "1.0.0\n"), ("app/catalog.py", "x = 1\n"), ("app/server.py", "y = 1\n")):
        with open(os.path.join(root, f), "w") as fh:
            fh.write(t)
    git(root, "init", "-q")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    monkeypatch.setattr(config, "ROOT", root)
    return root


def test_reads_the_version_file(repo):
    assert updater.current() == "1.0.0"


def test_local_changes_keep_full_file_names(repo):
    """Regression: the first file lost its first letter ("pp/catalog.py")."""
    with open(os.path.join(repo, "app", "catalog.py"), "a") as fh:
        fh.write("# edited\n")
    with open(os.path.join(repo, "untracked.txt"), "w") as fh:
        fh.write("new")
    assert updater.local_changes() == ["app/catalog.py"]


def test_status_offers_the_automatic_update_only_when_clean(repo, monkeypatch):
    monkeypatch.setitem(updater.state, "latest", "1.1.0")
    s = updater.status()
    assert s["available"] and s["git"] and s["auto"] and s["root"] == repo
    with open(os.path.join(repo, "app", "server.py"), "a") as fh:
        fh.write("# edited\n")
    s = updater.status()
    assert not s["auto"] and s["changes"] == ["app/server.py"]
    ok, msg = updater.update()
    assert not ok and "modifications locales" in msg


def test_no_update_when_up_to_date(repo, monkeypatch):
    monkeypatch.setitem(updater.state, "latest", "1.0.0")
    assert not updater.status()["available"]
    assert updater.update() == (False, "déjà à jour")


def test_zip_install_gets_the_install_command(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", str(tmp_path))
    (tmp_path / "VERSION").write_text("1.0.0")
    monkeypatch.setitem(updater.state, "latest", "1.1.0")
    s = updater.status()
    assert s["available"] and not s["git"] and not s["auto"] and ("install.ps1" if os.name == "nt" else "install.sh") in s["command"]


def test_offline_check_is_not_an_error(monkeypatch):
    monkeypatch.setattr(updater, "REMOTE_VERSION", "http://127.0.0.1:9/VERSION")
    updater.check(force=True)
    assert updater.state["error"]
