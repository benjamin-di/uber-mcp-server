"""The saved session: generations, sealing, write-back of renewed cookies, and what is kept out of it."""

from __future__ import annotations

import base64
import json
import os

import pytest
from conftest import COOKIES

from uber_mcp.errors import UberError
from uber_mcp.session import Session, clean_cookies, from_jar

ACCOUNT = {"uuid": "00000000-0000-4000-8000-000000000001", "name": "Alex"}
KEY = base64.b64decode(base64.b64encode(b"k" * 32))


def test_generations_rise_with_each_sign_in(tmp_path):
    s = Session(tmp_path / "session.json")
    assert s.write(ACCOUNT, "UA", COOKIES) == 1
    assert s.write(ACCOUNT, "UA", COOKIES) == 2
    assert s.status()["generation"] == 2 and s.status()["account"] == "A•••"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_the_file_is_private(tmp_path):
    Session(tmp_path / "session.json").write(ACCOUNT, "UA", COOKIES)
    assert (os.stat(tmp_path / "session.json").st_mode & 0o077) == 0


def test_sealed_sessions(tmp_path):
    path = tmp_path / "session.bin"
    Session(path, KEY).write(ACCOUNT, "UA", COOKIES)
    raw = path.read_bytes()
    assert raw.startswith(b"UBERS1") and b"jwt-session" not in raw
    assert Session(path, KEY).checkout()[0] == 1
    with pytest.raises(UberError) as exc:
        Session(path, b"x" * 32).checkout()
    assert exc.value.code == "session_invalid"
    with pytest.raises(UberError) as exc:
        Session(path).checkout()  # an unsealed reader of a sealed file
    assert exc.value.code == "session_invalid"


def test_missing_and_damaged(tmp_path):
    with pytest.raises(UberError) as exc:
        Session(tmp_path / "session.json").checkout()
    assert exc.value.code == "not_signed_in"
    (tmp_path / "session.json").write_text("{not json")
    with pytest.raises(UberError) as exc:
        Session(tmp_path / "session.json").checkout()
    assert exc.value.code == "session_invalid"


def test_renewed_cookies_are_written_back_to_the_same_generation(tmp_path):
    s = Session(tmp_path / "session.json")
    s.write(ACCOUNT, "UA", COOKIES)
    generation, jar, _ = s.checkout()
    for c in jar:
        if c.name == "jwt-session":
            c.value = "j2"
    s.store_jar(jar, generation)
    saved = json.loads((tmp_path / "session.json").read_text())
    assert saved["generation"] == 1 and {c["name"]: c["value"] for c in saved["cookies"]}["jwt-session"] == "j2"


def test_a_write_back_never_overwrites_a_newer_sign_in(tmp_path):
    s = Session(tmp_path / "session.json")
    s.write(ACCOUNT, "UA", COOKIES)
    generation, jar, _ = s.checkout()
    Session(tmp_path / "session.json").write(ACCOUNT, "UA2", COOKIES)  # a sign-in from another process
    for c in jar:
        c.value = "stale"
    s.store_jar(jar, generation)
    saved = json.loads((tmp_path / "session.json").read_text())
    assert saved["generation"] == 2 and "stale" not in json.dumps(saved)
    assert s.checkout()[0] == 2


def test_only_ubers_cookies_are_kept():
    kept = clean_cookies(
        [
            *COOKIES,
            {"name": "x", "value": "1", "domain": ".example.com"},
            {"name": "y", "value": "1", "domain": "eviluber.com"},
            {"name": "bad name", "value": "1", "domain": ".uber.com"},
            {"name": "z", "value": "line\nbreak", "domain": ".uber.com"},
        ]
    )
    assert {c["name"] for c in kept} == {"sid", "csid", "jwt-session"}


def test_the_jar_round_trip(tmp_path):
    s = Session(tmp_path / "session.json")
    s.write(ACCOUNT, "UA", COOKIES)
    _, jar, ua = s.checkout()
    assert ua == "UA" and sorted(c["name"] for c in from_jar(jar)) == sorted(c["name"] for c in COOKIES)
