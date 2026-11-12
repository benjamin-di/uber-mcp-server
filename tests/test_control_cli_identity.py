import pytest

from uber_mcp import connection_state


def test_control_initialization_arguments_are_attributed_and_redacted(capsys):
    with pytest.raises(SystemExit) as failure:
        connection_state.main(["--private-sentinel"])
    assert failure.value.code == 2
    output = capsys.readouterr().err
    assert "source_revision=" in output and "private-sentinel" not in output


def test_control_initialization_failure_is_attributed(monkeypatch, capsys):
    def fail(*args):
        raise ValueError("private-sentinel")

    monkeypatch.setattr(connection_state, "initialize", fail)
    assert connection_state.main(["--initialize"]) == 1
    output = capsys.readouterr().err
    assert "Connection initialization failed" in output and "private-sentinel" not in output
