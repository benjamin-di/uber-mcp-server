"""Lost authority must never turn an adopted account back into an active one."""

import shutil

import pytest

from uber_mcp.connection_state import ConnectionState
from uber_mcp.errors import UberError


def construct(folder, **kwargs):
    return ConnectionState(folder, **kwargs)


def bootstrap(folder):
    return construct(folder, require_existing=False).ensure()


def adopt(folder):
    bootstrap(folder)
    control = construct(folder)
    with control.locked() as value:
        control.save({**value, "state": "disconnected"})
    return control


def test_saved_disconnect_cannot_be_recreated_after_record_loss(tmp_path):
    folder = tmp_path / "state"
    control = adopt(folder)
    control.path.unlink()
    with pytest.raises(UberError):
        construct(folder).require_active()
    with pytest.raises(UberError):
        bootstrap(folder)


@pytest.mark.parametrize("setting", ["1", "invalid", ""])
def test_hosted_missing_or_replaced_state_requires_explicit_adoption(tmp_path, monkeypatch, setting):
    monkeypatch.setenv("CONNECTIONS_REQUIRE_CONTROLS", setting)
    folder = tmp_path / "state"
    with pytest.raises(UberError):
        construct(folder).read()
    bootstrap(folder)
    construct(folder).require_active()
    control = adopt(folder)
    before = control.path.read_bytes()
    bootstrap(folder)
    assert control.path.read_bytes() == before
    shutil.rmtree(folder)
    folder.mkdir(mode=0o700)
    with pytest.raises(UberError):
        construct(folder).require_active()
