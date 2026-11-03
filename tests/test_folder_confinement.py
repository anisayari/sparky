from contextlib import contextmanager
import os
from pathlib import Path
import subprocess

import pytest

import rag
import rag_sync


def directory_link(link, target):
    if os.name == "nt":
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        "$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path $env:QA_LINK -Target $env:QA_TARGET | Out-Null"],
                       env=dict(os.environ, QA_LINK=str(link), QA_TARGET=str(target)), check=True, capture_output=True, timeout=15)
    else:
        link.symlink_to(target, target_is_directory=True)


def test_parent_replaced_by_link_after_authorization(tmp_path):
    outer = tmp_path / "parent"
    root = outer / "chosen"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "chosen").mkdir(parents=True)
    (root / "document.txt").write_text("authorized")
    (outside / "chosen" / "document.txt").write_text("private sentinel")
    chosen = rag_sync._folder(root)
    assert rag_sync._read_file(chosen, Path("document.txt"))[0] == b"authorized"
    outer.rename(tmp_path / "original")
    directory_link(outer, outside)
    with pytest.raises((OSError, ValueError)):
        rag_sync._read_file(chosen, Path("document.txt"))
    errors = []
    assert not rag_sync._scan_files(chosen, errors.append)
    assert errors
    with pytest.raises(ValueError):
        rag_sync._folder(root)


def test_subdirectory_substituted_between_discovery_and_descent(tmp_path, monkeypatch):
    root = tmp_path / "chosen"
    child = root / "nested"
    child.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private-name.txt").write_text("not indexed")
    original = rag_sync._directory
    errors = []

    @contextmanager
    def swap_on_descent(path):
        if path == child:
            child.rename(tmp_path / "moved")
            directory_link(child, outside)
        with original(path) as handle:
            yield handle

    monkeypatch.setattr(rag_sync, "_directory", swap_on_descent)
    assert not rag_sync._scan_files(root, errors.append)
    assert errors


def test_open_directory_stays_attached_during_parent_rename(tmp_path):
    outer = tmp_path / "parent"
    root = outer / "chosen"
    root.mkdir(parents=True)
    (root / "authorized.txt").touch()
    with rag_sync._directory(root) as anchor:
        if os.name == "nt":
            with pytest.raises(PermissionError):
                outer.rename(tmp_path / "moved")
        else:
            outer.rename(tmp_path / "moved")
            root.mkdir(parents=True)
            (root / "replacement.txt").touch()
        with os.scandir(anchor) as entries:
            assert [entry.name for entry in entries] == ["authorized.txt"]


def test_leaf_replaced_with_link_is_never_followed(tmp_path, monkeypatch):
    root = tmp_path / "chosen"
    root.mkdir()
    path = root / "note.txt"
    path.write_bytes(b"approved")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "private.txt"
    sentinel.write_bytes(b"outside the selected root")
    assert "note.txt" in rag_sync._scan_files(root, pytest.fail)
    path.unlink()
    try:
        path.symlink_to(sentinel)
    except OSError as exc:
        if os.name != "nt" or exc.winerror != 1314:
            raise
        # A Windows workstation may lack file symlink privileges. A directory
        # junction swapped into the same leaf exercises the same reparse check.
        directory_link(path, outside)
    opens = []
    if os.name == "nt":
        create = rag_sync._create_file

        def checked(name, access, share, security, disposition, flags, template):
            if Path(name) == path:
                opens.append(flags)
                assert flags & 0x00200000  # OPEN_REPARSE_POINT on the initial open
            return create(name, access, share, security, disposition, flags, template)

        monkeypatch.setattr(rag_sync, "_create_file", checked)
    monkeypatch.setattr(rag_sync, "_bounded_read", lambda *_: pytest.fail("read attempted on substituted leaf"))
    for stamp_only in (False, True):
        with pytest.raises((OSError, ValueError)):
            rag_sync._read_file(root, Path("note.txt"), stamp_only=stamp_only)
    if os.name == "nt":
        assert len(opens) == 2


def test_parent_traversal_never_authorized(tmp_path):
    root = tmp_path / "chosen"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("secret")
    with pytest.raises(ValueError):
        rag_sync._read_file(root, Path("../outside.txt"))
    with pytest.raises(ValueError):
        rag_sync._folder(root / ".." / "chosen")


def test_same_size_edit_with_restored_mtime_revokes_indexed_document(tmp_path):
    root = tmp_path / "chosen"
    root.mkdir()
    source = root / "notes.txt"
    source.write_bytes(b"before!")
    info = source.stat()
    cid = rag.create_collection("mtime regression")
    try:
        rag_sync.set_folder(cid, str(root))
        rag_sync.scan(cid, str(root))
        old, = rag._read("SELECT id,sync_stamp FROM documents WHERE collection_id=?", (cid,))
        source.write_bytes(b"edited!")
        os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
        assert (source.stat().st_size, source.stat().st_mtime_ns) == (info.st_size, info.st_mtime_ns)
        rag_sync.scan(cid, str(root))
        new, = rag._read("SELECT id,sync_stamp FROM documents WHERE collection_id=?", (cid,))
        assert new["id"] != old["id"] and new["sync_stamp"] != old["sync_stamp"]
        rag_sync.scan(cid, str(root))
        assert rag._read("SELECT id FROM documents WHERE collection_id=?", (cid,))[0]["id"] == new["id"]
    finally:
        rag.delete_collection(cid)
