"""How Uber's answers are classified: signed out, pushed back, changed, down. And pacing state: budget, pause."""

from __future__ import annotations

import json

import pytest
from conftest import make_service
from fixtures import uber as fx

from uber_mcp.errors import UberError


def code(service, op="GetStatus", variables=None) -> str:
    with pytest.raises(UberError) as exc:
        service.uber.graphql(op, variables or {"latitude": 0, "longitude": 0})
    return exc.value.code


def test_a_read_as_the_rider_proves_the_session(service):
    assert service.session.status()["state"] == "unverified"
    service.account.user(fresh=True)
    assert service.session.status()["signed_in"] is True


def test_another_rider_than_the_one_signed_in_is_refused(service, fake):
    other = fx.user()
    other["currentUser"]["uuid"] = "00000000-0000-4000-8000-0000000000ff"
    fake.override["CurrentUserRidersWeb"] = {"data": other}
    assert code(service, "CurrentUserRidersWeb", {}) == "session_invalid"


@pytest.mark.parametrize(
    "answer",
    [
        (401, "{}"),
        (403, '{"error": "forbidden"}'),
        (200, json.dumps({"errors": [{"message": "x", "extensions": {"code": "unauthenticated"}}], "data": None})),
    ],
)
def test_signed_out_answers_end_the_session(service, fake, answer):
    fake.override["GetStatus"] = lambda _: answer
    assert code(service) == "session_expired"
    assert service.session.status()["state"] == "dead"
    fake.override.clear()
    assert code(service) == "session_expired"  # a dead generation stays dead until a new sign-in
    assert len(fake.sent) == 1


def test_riders_404_is_signed_out(service, fake):
    fake.override["Activities"] = lambda _: (404, "<html>")
    assert code(service, "Activities", {}) == "session_expired"


def test_a_new_sign_in_replaces_a_dead_session(service, fake, tmp_path):
    fake.override["GetStatus"] = lambda _: (401, "{}")
    code(service)
    fake.override.clear()
    from conftest import COOKIES

    service.session.write({"uuid": fx.RIDER, "name": "Alex"}, "UA", COOKIES)
    assert service.uber.graphql("GetStatus", {"latitude": 0, "longitude": 0})["status"]["clientStatus"] == "Looking"


def test_429_pauses_everything(service, fake):
    fake.override["GetStatus"] = lambda _: (429, "slow down")
    assert code(service) == "blocked"
    fake.override.clear()
    assert code(service) == "blocked"  # paused: nothing sent
    assert len(fake.sent) == 1
    assert service.state.snapshot()["paused_until"] is not None


def test_a_challenge_page_pauses_and_keeps_the_session(service, fake):
    fake.override["GetStatus"] = lambda _: (403, "<!DOCTYPE html><html>challenge-platform</html>")
    assert code(service) == "blocked"
    assert service.session.status()["state"] != "dead"


def test_changed_documents_are_schema_changed(service, fake):
    fake.override["GetStatus"] = {"errors": [{"message": 'Cannot query field "x" on type "Status"'}], "data": None}
    assert code(service) == "schema_changed"
    fake.override["GetStatus"] = lambda _: (200, "not json")
    assert code(service) == "schema_changed"


def test_server_errors_are_upstream(service, fake):
    fake.override["GetStatus"] = lambda _: (502, "bad gateway")
    assert code(service) == "upstream_error"


def test_the_budget(tmp_path, fake):
    svc = make_service(tmp_path, fake, UBER_DAILY_BUDGET="2")
    svc.uber.graphql("GetStatus", {"latitude": 0, "longitude": 0})
    svc.uber.graphql("GetStatus", {"latitude": 0, "longitude": 0})
    assert code(svc) == "budget_exhausted"
    assert len(fake.sent) == 2


def test_not_signed_in(tmp_path, fake):
    svc = make_service(tmp_path, fake, signed_in=False)
    assert code(svc) == "not_signed_in"
    assert fake.sent == []


def test_requests_carry_the_browser_identity_and_csrf_header(service, fake):
    seen = {}

    def capture(jar):
        opener = fake.opener(jar)

        class Wrap:
            def open(self, req, timeout=0):
                seen.update(dict(req.header_items()))
                return opener.open(req, timeout)

        return Wrap()

    service.uber.transport._opener = capture
    service.uber.graphql("GetStatus", {"latitude": 0, "longitude": 0})
    headers = {k.lower(): v for k, v in seen.items()}
    assert headers["x-csrf-token"] == "x" and headers["user-agent"] == "Mozilla/5.0 (test)"
    assert headers["origin"] == "https://m.uber.com"
