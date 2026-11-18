import hashlib
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def bootstrap():
    return runpy.run_path(str(Path(__file__).resolve().parents[2] / "tools/handoff/bootstrap.py"))


def test_verified_installer_is_pinned_to_selected_release(bootstrap, monkeypatch, tmp_path, capsys):
    function = bootstrap["refresh_installer"]
    source = tmp_path / "old.py"
    source.write_bytes(b"old")
    raw = b"new verified installer"
    manifest = {"artifacts": {"bootstrap.py": hashlib.sha256(raw).hexdigest()}}
    calls = []
    monkeypatch.setitem(function.__globals__, "__file__", str(source))
    monkeypatch.setitem(function.__globals__, "fetch", lambda url: raw)
    monkeypatch.setitem(function.__globals__, "run", lambda args: calls.append(args) or SimpleNamespace(stdout="installed\n"))
    monkeypatch.setattr("sys.argv", ["bootstrap.py", "hub", "--release", "latest"])
    assert function({"tag_name": "v1.2.3"}, {"bootstrap.py": "https://example.test/bootstrap.py"}, manifest)
    assert calls[0][-2:] == ["--release", "v1.2.3"]
    assert not Path(calls[0][1]).exists()
    assert capsys.readouterr().out == "installed\n"


def test_unverified_installer_is_never_executed(bootstrap, monkeypatch, tmp_path):
    function = bootstrap["refresh_installer"]
    source = tmp_path / "old.py"
    source.write_bytes(b"old")
    monkeypatch.setitem(function.__globals__, "__file__", str(source))
    monkeypatch.setitem(function.__globals__, "fetch", lambda url: b"tampered")
    monkeypatch.setitem(function.__globals__, "run", lambda args: pytest.fail("Unverified installer executed"))
    with pytest.raises(RuntimeError, match="checksum"):
        function({"tag_name": "v1.2.3"}, {"bootstrap.py": "https://example.test/bootstrap.py"},
                 {"artifacts": {"bootstrap.py": hashlib.sha256(b"expected").hexdigest()}})


def test_matching_installer_does_not_delegate(bootstrap, monkeypatch):
    function = bootstrap["refresh_installer"]
    current = Path(function.__globals__["__file__"]).read_bytes()
    monkeypatch.setitem(function.__globals__, "fetch", lambda url: pytest.fail("Unexpected download"))
    assert not function({}, {}, {"artifacts": {"bootstrap.py": hashlib.sha256(current).hexdigest()}})
