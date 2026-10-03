def test_no_sessions_table_through_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    hermes = tmp_path / "hermes"
    (hermes / "hermes_cli").mkdir(parents=True)
    (hermes / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
    (hermes / "hermes_cli" / "main.py").write_text("def main(): return 0\n", encoding="utf-8")
    (hermes / "hermes_cli" / "oneshot.py").write_text(_STAND_IN_ONESHOT, encoding="utf-8")
    metadata = hermes / "hermes_agent-0.19.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: hermes-agent\nVersion: 0.19.0\n", encoding="utf-8")
    
    home = tmp_path / "home"
    home.mkdir()
    with sqlite3.connect(home / "state.db") as db:
        pass
        
    path = tmp_path / USAGE_NAME
    path.write_text(json.dumps(RAISED_USAGE), encoding="utf-8")
    
    wrapper = _wrapper()
    wrapper.HERMES_PYTHON = sys.executable
    
    monkeypatch.setenv("CRUCIBLE_HERMES_USAGE", str(path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("PYTHONPATH", str(hermes))
    monkeypatch.setattr(sys, "argv", ["crucible-hermes.py"])
    
    assert wrapper.main() == 0
    
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == {**RAISED_USAGE, "completed": False}
    
    assert "crucible-hermes: Hermes's state.db has no sessions table" in capsys.readouterr().err
