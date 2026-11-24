"""Synthetic money boundaries. Real Transport, fake opener, isolated state; no keepalive or live service."""

from __future__ import annotations

import errno
import json
import multiprocessing
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event

import pytest
from conftest import FakeUber, make_service
from fixtures import uber as fx
from test_booking import ARGS, code, preview
from test_reservations import RESERVATION

from uber_mcp import booking, fsio
from uber_mcp.booking import WINDOW, Ledger
from uber_mcp.errors import UberError

pytest_plugins = ["test_reservations"]

CAPS = {"UBER_MAX_FARE": "XXD 100", "UBER_MAX_FARE_24H": "XXD 150"}


def lost(_):
    raise OSError("synthetic lost reply")


def held(svc):
    return next(iter(svc.ledger.actions().values()))


@pytest.mark.parametrize("effect", ["accepted", "refused", "unknown"])
@pytest.mark.parametrize("age", [601, WINDOW - 0.001, WINDOW, WINDOW + 1])
def test_restart_replay_ignores_age_and_freeze_but_does_no_provider_io(tmp_path, fake, effect, age):
    svc = make_service(tmp_path, fake, **CAPS)
    now = svc.ledger._clock()
    svc.ledger._clock = lambda: now
    token = preview(svc)["confirm_token"]
    if effect == "unknown":
        fake.override["TripRequest"] = lost
    elif effect == "refused":
        fake.override["TripRequest"] = {"data": {"tripRequest": {"errorCode": "EXAMPLE_REFUSAL"}}}
    try:
        original = svc.request_ride(*ARGS, token)
    except UberError as exc:
        original = exc.payload()
    restarted = make_service(tmp_path, fake, signed_in=False, **CAPS)
    restarted.ledger._clock = lambda: now + age
    restarted.ledger.freeze("synthetic freeze")
    before = len(fake.sent)
    try:
        result = restarted.request_ride(*ARGS, token)
    except UberError as exc:
        result = exc.payload()
    assert result == original and len(fake.sent) == before
    expected = 5000 if age < WINDOW and effect in {"accepted", "unknown"} else 0
    assert restarted.ledger.spent_24h("XXD") == expected
    assert held(restarted)["at"] == now and held(restarted)["effect"] == effect


def test_replay_is_input_kind_and_account_scoped(tmp_path, booking_service, fake):
    svc = booking_service
    token = preview(svc)["confirm_token"]
    result = svc.request_ride(*ARGS, token)
    before = len(fake.sent)
    assert code(svc.request_ride, "other", *ARGS[1:], token) == "confirm_invalid"
    assert code(svc.cancel_ride, token) == "confirm_invalid"
    other = make_service(tmp_path / "other", fake, **CAPS)
    assert code(other.request_ride, *ARGS, token) == "confirm_invalid"
    assert len(fake.sent) == before
    assert svc.request_ride(*ARGS, token) == result


def test_price_rise_replay_keeps_the_original_new_approval(booking_service, fake):
    svc = booking_service
    token = preview(svc)["confirm_token"]
    fake.prices["UberX"] = 5500
    with pytest.raises(UberError) as first:
        svc.request_ride(*ARGS, token)
    before = len(fake.sent)
    with pytest.raises(UberError) as second:
        svc.request_ride(*ARGS, token)
    assert first.value.code == "price_rose"
    assert second.value.payload() == first.value.payload() and len(fake.sent) == before
    assert svc.ledger.spent_24h("XXD") == 0


@pytest.mark.parametrize("change", ["default", "removed", "profile", "method"])
def test_on_demand_payment_is_the_exact_approved_method(booking_service, fake, change):
    svc = booking_service
    token = preview(svc)["confirm_token"]
    user = fx.user()
    if change == "default":
        user["currentUser"]["lastSelectedPaymentProfileUuid"] = fx.PAYMENT_CASH
    elif change == "removed":
        user["currentUser"]["paymentProfiles"] = []
    elif change == "method":
        user["currentUser"]["paymentProfiles"][0]["tokenType"] = "changed-example-type"
    else:
        # Alter the actual selected profile after the preview, not the cached Account value.
        for profile in user["currentUser"]["profiles"]:
            profile["uuid"] = "00000000-0000-4000-8000-0000000000ff"
    fake.override["CurrentUserRidersWeb"] = {"data": user}
    if change == "default":
        assert svc.request_ride(*ARGS, token)["requested"]
        sent = next(v for op, _, v in fake.sent if op == "TripRequest")
        assert sent["payment"]["paymentProfileUUID"] == fx.PAYMENT_CARD
    else:
        assert code(svc.request_ride, *ARGS, token) in {"payment_not_found", "terms_changed", "schema_changed"}
        assert "TripRequest" not in fake.ops()


def test_freeze_during_quote_is_checked_in_atomic_admission(booking_service, fake):
    svc = booking_service
    token = preview(svc)["confirm_token"]

    def freeze(_):
        svc.ledger.freeze("during quote")
        return {"data": fx.products({})}

    fake.override["Products"] = freeze
    assert code(svc.request_ride, *ARGS, token) in {"booking_disabled", "confirm_invalid"}
    assert "TripRequest" not in fake.ops() and svc.ledger.spent_24h("XXD") == 0


@pytest.mark.parametrize(
    "status,envelope,effect",
    [
        (200, {"data": {"tripRequest": {"errorCode": "EXAMPLE_REFUSAL"}}}, "refused"),
        (200, {"data": {"tripRequest": {"errorKey": "example.refusal"}}}, "unknown"),
        (200, {"data": {"tripRequest": {"errorCode": []}}}, "unknown"),
        (200, {"data": {"tripRequest": {"errorCode": "bad code"}}}, "unknown"),
        (200, {"data": {"tripRequest": {"uuid": fx.TRIP, "errorCode": "REFUSAL"}}}, "unknown"),
        (200, {"data": {"tripRequest": {"uuid": fx.TRIP, "plusOne": {"x": 1}}}}, "unknown"),
        (200, {"data": {"tripRequest": {"errorCode": "REFUSAL", "checkoutActionParameters": {"x": 1}}}}, "unknown"),
        (200, {"data": {"tripRequest": {"errorCode": "REFUSAL"}}, "errors": [{"message": "partial"}]}, "unknown"),
        (200, {"data": {"tripRequest": {"uuid": fx.TRIP}}, "errors": [{"message": "partial"}]}, "unknown"),
        (418, {"data": {"tripRequest": {"errorCode": "REFUSAL"}}}, "unknown"),
        (401, {}, "unknown"),
        (403, {}, "unknown"),
        (429, {}, "unknown"),
        (503, {}, "unknown"),
        (302, {"data": {"tripRequest": {"uuid": fx.TRIP}}}, "unknown"),
        (200, {"errors": [{"extensions": {"code": "UNAUTHENTICATED"}}]}, "unknown"),
        (200, {"data": {"tripRequest": None}}, "unknown"),
    ],
)
def test_money_envelope_and_operation_evidence(booking_service, fake, status, envelope, effect):
    svc = booking_service
    token = preview(svc)["confirm_token"]
    fake.override["TripRequest"] = (status, json.dumps(envelope))
    assert code(svc.request_ride, *ARGS, token) == ("uber_refused" if effect == "refused" else "outcome_unknown")
    assert held(svc)["effect"] == effect
    assert svc.ledger.spent_24h("XXD") == (0 if effect == "refused" else 5000)
    before = len(fake.sent)
    assert code(svc.request_ride, *ARGS, token) in {"uber_refused", "outcome_unknown"}
    assert len(fake.sent) == before


@pytest.mark.parametrize("remote", [False, True])
def test_same_blocked_code_on_opposite_sides_of_open(booking_service, fake, monkeypatch, remote):
    svc = booking_service
    token = preview(svc)["confirm_token"]
    transport = svc.uber.transport
    original = svc.ledger.begin_action

    def begin(*args, **kwargs):
        value = original(*args, **kwargs)
        if not remote:
            monkeypatch.setattr(transport.state, "check_pause", lambda: (_ for _ in ()).throw(UberError("blocked")))
        return value

    monkeypatch.setattr(svc.ledger, "begin_action", begin)
    if remote:
        fake.override["TripRequest"] = (429, "{}")
    assert code(svc.request_ride, *ARGS, token) == ("outcome_unknown" if remote else "blocked")
    assert svc.ledger.spent_24h("XXD") == (5000 if remote else 0)
    assert fake.ops().count("TripRequest") == int(remote)


def inject_write_fault(monkeypatch, svc, token, stage, final):
    original = booking.atomic_write_json

    def write(path, data, **kwargs):
        a = data.get("action_attempts", {}).get(token)
        match = a is not None and ((a["effect"] == "accepted") if final else (a["effect"] == "unknown"))
        if path != svc.ledger.path or not match:
            return original(path, data, **kwargs)

        def fail(*args, **kw):
            raise OSError(errno.EIO, "synthetic storage fault")

        with monkeypatch.context() as m:
            if stage == "temporary":
                m.setattr(fsio.tempfile, "mkstemp", fail)
            elif stage == "write":
                fdopen = fsio.os.fdopen

                @contextmanager
                def failed_stream(fd, mode):
                    with fdopen(fd, mode):

                        class BrokenStream:
                            write = fail

                        yield BrokenStream()

                m.setattr(fsio.os, "fdopen", failed_stream)
            elif stage == "replace":
                m.setattr(fsio.os, "replace", fail)
            else:
                sync = fsio.os.fsync

                def fsync(fd):
                    directory = stat.S_ISDIR(os.fstat(fd).st_mode)
                    if directory == (stage == "directory"):
                        fail()
                    return sync(fd)

                m.setattr(fsio.os, "fsync", fsync)
            return original(path, data, **kwargs)

    monkeypatch.setattr(booking, "atomic_write_json", write)


@pytest.mark.parametrize("stage", ["temporary", "write", "file", "replace", "directory"])
def test_failed_intent_write_never_dispatches(booking_service, fake, monkeypatch, stage):
    if stage == "directory" and os.name == "nt":
        pytest.skip("directory fsync is unsupported on Windows")
    svc = booking_service
    token = preview(svc)["confirm_token"]
    inject_write_fault(monkeypatch, svc, token, stage, False)
    assert code(svc.request_ride, *ARGS, token) == (
        "outcome_unknown" if stage == "directory" else "booking_state_invalid"
    )
    assert "TripRequest" not in fake.ops()
    monkeypatch.undo()
    restarted = Ledger(svc.settings.state_dir)
    if stage == "directory" and os.name != "nt":
        assert restarted.spent_24h("XXD") == 5000
        assert code(svc.request_ride, *ARGS, token) == "outcome_unknown"
    else:
        assert restarted.actions() == {} and restarted.spent_24h("XXD") == 0
        assert svc.request_ride(*ARGS, token)["requested"]


@pytest.mark.parametrize("stage", ["temporary", "write", "file", "replace", "directory"])
def test_final_write_failure_preserves_strongest_visible_record_without_double_transfer(
    booking_service, fake, monkeypatch, stage
):
    if stage == "directory" and os.name == "nt":
        pytest.skip("directory fsync is unsupported on Windows")
    svc = booking_service
    token = preview(svc)["confirm_token"]
    inject_write_fault(monkeypatch, svc, token, stage, True)
    assert code(svc.request_ride, *ARGS, token) == "outcome_unknown"
    monkeypatch.undo()
    a = held(svc)
    assert a["effect"] == ("accepted" if stage == "directory" and os.name != "nt" else "unknown")
    before = len(fake.sent)
    if a["effect"] == "accepted":
        assert svc.request_ride(*ARGS, token)["requested"]
    else:
        assert code(svc.request_ride, *ARGS, token) == "outcome_unknown"
    assert len(fake.sent) == before and fake.ops().count("TripRequest") == 1
    assert svc.ledger.spent_24h("XXD") == 5000
    data = json.loads(svc.ledger.path.read_text())
    assert len(data["booked"]) == (a["effect"] == "accepted")
    if data["booked"]:
        assert data["booked"][0]["at"] == a["at"]


def test_audit_write_error_cannot_mask_acceptance(booking_service, fake, monkeypatch):
    svc = booking_service
    token = preview(svc)["confirm_token"]
    original = booking.os.open

    def opening(path, *args, **kwargs):
        if Path(path) == svc.ledger.audit_path:
            raise OSError(errno.ENOSPC, "synthetic audit full")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(booking.os, "open", opening)
    assert svc.request_ride(*ARGS, token)["requested"]
    assert svc.ledger.spent_24h("XXD") == 5000


def test_cookie_outer_lock_error_preserves_money_ack(booking_service, fake, monkeypatch):
    svc = booking_service
    token = preview(svc)["confirm_token"]
    original = svc.session.connection.locked
    entered = False

    def accepted(_):
        nonlocal entered
        entered = True
        return {"data": {"tripRequest": {"uuid": fx.TRIP}}}

    @contextmanager
    def locking():
        if entered:
            raise OSError(errno.EIO, "synthetic outer cookie lock")
        with original() as value:
            yield value

    fake.override["TripRequest"] = accepted
    monkeypatch.setattr(svc.session.connection, "locked", locking)
    assert svc.request_ride(*ARGS, token)["requested"]
    assert svc.ledger.spent_24h("XXD") == 5000


@pytest.mark.parametrize("when", ["before_checkout", "after_open"])
def test_disconnect_never_erases_effect_facts(booking_service, fake, monkeypatch, when):
    svc = booking_service
    token = preview(svc)["confirm_token"]
    if when == "before_checkout":
        original = svc.ledger.begin_action

        def begin(*args, **kwargs):
            a = original(*args, **kwargs)
            svc.session.connection.disconnect(svc.session)
            return a

        monkeypatch.setattr(svc.ledger, "begin_action", begin)
        assert code(svc.request_ride, *ARGS, token) == "connection_disconnected"
        assert held(svc)["effect"] == "not_submitted" and "TripRequest" not in fake.ops()
    else:

        def accepted(_):
            svc.session.connection.disconnect(svc.session)
            return {"data": {"tripRequest": {"uuid": fx.TRIP}}}

        fake.override["TripRequest"] = accepted
        assert svc.request_ride(*ARGS, token)["requested"]
        assert held(svc)["effect"] == "accepted"
    before = len(fake.sent)
    assert code(svc.request_ride, *ARGS, token) == "connection_disconnected"
    assert len(fake.sent) == before


def test_late_resolution_uses_independent_correlation_and_exact_returned_id(booking_service, fake):
    svc = booking_service
    now = svc.ledger._clock()
    svc.ledger._clock = lambda: now
    token = preview(svc)["confirm_token"]
    fake.override["TripRequest"] = lost
    assert code(svc.request_ride, *ARGS, token) == "outcome_unknown"
    operation = held(svc)["id"]
    other = "00000000-0000-4000-8000-0000000000ff"
    with pytest.raises(UberError):
        svc.booking.resolve_action(operation, target=other, evidence="independent provider support receipt")
    assert held(svc)["effect"] == "unknown"
    svc.ledger._clock = lambda: now + WINDOW + 1
    svc.booking.resolve_action(
        operation, target=fx.TRIP, evidence="provider support independently correlated operation"
    )
    assert svc.ledger.spent_24h("XXD") == 0
    assert held(svc)["evidence"]["correlated_target"] == fx.TRIP
    before = len(fake.sent)
    assert svc.request_ride(*ARGS, token)["trip_id"] == fx.TRIP
    assert len(fake.sent) == before
    with pytest.raises(UberError):
        svc.booking.resolve_action(operation, target=fx.TRIP, evidence="duplicate resolution")
    assert fake.ops().count("TripRequest") == 1


def test_proven_non_submission_releases_once_and_inspection_needs_no_session(booking_service, fake):
    svc = booking_service
    token = preview(svc)["confirm_token"]
    fake.override["TripRequest"] = lost
    assert code(svc.request_ride, *ARGS, token) == "outcome_unknown"
    operation = held(svc)["id"]
    svc.session.connection.disconnect(svc.session)
    before = len(fake.sent)
    rows = Ledger(svc.settings.state_dir).held_actions()
    assert rows[0]["id"] == operation and "approval" not in rows[0] and "binding" not in rows[0]
    svc.booking.resolve_action(operation, not_submitted=True, evidence="authoritative provider non-submission receipt")
    assert svc.ledger.spent_24h("XXD") == 0 and len(fake.sent) == before
    with pytest.raises(UberError):
        svc.booking.resolve_action(operation, not_submitted=True, evidence="duplicate")


@pytest.mark.parametrize("result", [True, False, None, "lost"])
def test_exact_current_cancel_target_blocks_second_token(booking_service, fake, result):
    svc = booking_service
    fake.trip = fx.live_trip()
    tokens = [svc.cancel_ride(None)["confirm_token"] for _ in range(2)]
    fake.override["TripCancel"] = lost if result == "lost" else {"data": {"tripCancel": result}}
    if result is True:
        assert svc.cancel_ride(tokens[0])["cancelled"]
    else:
        assert code(svc.cancel_ride, tokens[0]) == "outcome_unknown"
    # GetStatus deliberately stays stale/active, even after a true acknowledgement.
    assert code(svc.cancel_ride, tokens[1]) == "outcome_unknown"
    before = len(fake.sent)
    if result is True:
        assert svc.cancel_ride(tokens[0])["cancelled"]
    else:
        assert code(svc.cancel_ride, tokens[0]) == "outcome_unknown"
    assert len(fake.sent) == before and fake.ops().count("TripCancel") == 1


@pytest.mark.parametrize("ack", [True, False])
@pytest.mark.parametrize("verification", ["stale", "unavailable"])
def test_reservation_cancel_target_barrier_survives_ack_and_lookup_failure(reserve, fake, ack, verification):
    svc, args, raw = reserve
    svc.reserve_ride(confirm=svc.reserve_ride(**args)["confirm_token"])
    tokens = [svc.cancel_reservation(RESERVATION, None)["confirm_token"] for _ in range(2)]
    fake.override["TripCancel"] = {"data": {"tripCancel": True}} if ack else lost
    if verification == "unavailable":
        original = fake.override["GetReservation"]

        def dispatch(_):
            fake.override["GetReservation"] = {"data": {"getReservation": {"reservation": None}}}
            return {"data": {"tripCancel": True}} if ack else lost(None)

        fake.override["TripCancel"] = dispatch
    if ack:
        assert svc.cancel_reservation(RESERVATION, tokens[0])["cancellation_accepted"]
    else:
        assert code(svc.cancel_reservation, RESERVATION, tokens[0]) == "outcome_unknown"
    if verification == "unavailable":
        fake.override["GetReservation"] = original
    assert code(svc.cancel_reservation, RESERVATION, tokens[1]) == "outcome_unknown"
    assert fake.ops().count("TripCancel") == 1


@pytest.mark.parametrize(
    "terminal,attention",
    [("CANCELED", None), ("CANCELLED", None), ("FAILED", "reservation_failed"), ("EXPIRED", "reservation_failed")],
)
def test_unknown_cancel_then_exact_target_observation_is_not_fabricated_ack(reserve, fake, terminal, attention):
    svc, args, raw = reserve
    receipt = svc.reserve_ride(confirm=svc.reserve_ride(**args)["confirm_token"])
    token = svc.cancel_reservation(RESERVATION, None)["confirm_token"]
    fake.override["TripCancel"] = lost
    assert code(svc.cancel_reservation, RESERVATION, token) == "outcome_unknown"
    raw["reservationStatus"] = terminal
    svc.monitor_reservations()
    state = svc.ledger.attempts()[receipt["attempt_id"]]
    assert state["status"] == terminal and state["attention"] == attention
    assert held(svc)["acknowledged"] is False
    assert held(svc)["observed_cancelled"] == (terminal in {"CANCELED", "CANCELLED"})
    assert fake.ops().count("TripCancel") == 1


@pytest.mark.parametrize("branch", ["success", "error", "ageout", "conflict"])
def test_monitor_waiting_for_action_uses_fresh_terminal_state(reserve, fake, monkeypatch, branch):
    svc, args, raw = reserve
    receipt = svc.reserve_ride(confirm=svc.reserve_ride(**args)["confirm_token"])
    key = receipt["attempt_id"]
    token = svc.cancel_reservation(RESERVATION, None)["confirm_token"]
    waiting, resume = Event(), Event()
    original = svc.ledger.action

    @contextmanager
    def delayed():
        waiting.set()
        assert resume.wait(10)
        with original():
            yield

    # This monitor is poised before acquiring action. Cancel and observe first.
    with ThreadPoolExecutor(1) as pool, monkeypatch.context() as m:
        m.setattr(svc.ledger, "action", delayed)
        pending = pool.submit(svc.monitor_reservations)
        assert waiting.wait(10)
        m.undo()
        raw["reservationStatus"] = "CONFIRMED"
        assert svc.cancel_reservation(RESERVATION, token)["cancellation_accepted"]
        raw["reservationStatus"] = "CANCELED"
        with original():
            svc.booking._observe_target(RESERVATION, detail=svc.reservation_details(RESERVATION))
        if branch == "error":
            fake.override["GetReservation"] = {"data": {"getReservation": {"reservation": None}}}
        elif branch == "ageout":
            svc.ledger._clock = lambda: __import__("time").time() + 10 * 86400
        elif branch == "success":
            raw["reservationStatus"] = "CONFIRMED"  # stale remote data cannot regress terminal
        before = len(fake.sent)
        resume.set()
        pending.result(10)
    assert len(fake.sent) == before
    state = svc.ledger.attempts()[key]
    assert state["status"] == "CANCELED" and state["attention"] is None
    if branch == "conflict":
        with original():
            svc.booking._reservation_conflict(__import__("time").time() + 100000)
        assert svc.ledger.attempts()[key]["status"] == "CANCELED"


def process_confirm(directory, token, barrier, result, sink, crash):
    """Spawned interpreter, independent Ledger/Session/Transport and a durable synthetic effect sink."""
    fake = FakeUber()
    svc = make_service(Path(directory), fake, signed_in=False, UBER_MAX_FARE="XXD 75", UBER_MAX_FARE_24H="XXD 75")
    original = svc.ledger.begin_action

    def begin(*args, **kwargs):
        if crash == "before_intent":
            os._exit(31)
        a = original(*args, **kwargs)
        if crash == "after_intent":
            os._exit(32)
        return a

    svc.ledger.begin_action = begin

    def accept(_):
        with open(sink, "ab", buffering=0) as stream:
            stream.write(b"effect\n")
            os.fsync(stream.fileno())
        if crash == "after_acceptance":
            os._exit(33)
        return {"data": {"tripRequest": {"uuid": fx.TRIP}}}

    fake.override["TripRequest"] = accept
    barrier.wait(15)
    try:
        svc.request_ride(*ARGS, token)
        result.put("accepted")
    except UberError as exc:
        result.put(exc.code)


@pytest.mark.parametrize("same_token", [False, True])
def test_separate_processes_serialize_tokens_and_cap(tmp_path, same_token):
    fake = FakeUber()
    svc = make_service(tmp_path, fake, UBER_MAX_FARE="XXD 75", UBER_MAX_FARE_24H="XXD 75")
    first = preview(svc)["confirm_token"]
    second = first if same_token else preview(svc)["confirm_token"]
    ctx = multiprocessing.get_context("spawn")
    barrier, results, sink = ctx.Barrier(3), ctx.Queue(), tmp_path / "synthetic-effects"
    children = [
        ctx.Process(target=process_confirm, args=(str(tmp_path), token, barrier, results, str(sink), None))
        for token in (first, second)
    ]
    for child in children:
        child.start()
    barrier.wait(15)
    for child in children:
        child.join(20)
        if child.is_alive():
            child.terminate()
            child.join()
        assert child.exitcode == 0
    outcomes = sorted([results.get(timeout=5), results.get(timeout=5)])
    assert outcomes == (["accepted", "accepted"] if same_token else ["accepted", "over_cap"])
    assert sink.read_bytes() == b"effect\n" and svc.ledger.spent_24h("XXD") == 5000


@pytest.mark.parametrize(
    "crash,exit_code,effects", [("before_intent", 31, 0), ("after_intent", 32, 0), ("after_acceptance", 33, 1)]
)
def test_process_crash_and_reopen_never_resends_held_intent(tmp_path, crash, exit_code, effects):
    fake = FakeUber()
    svc = make_service(tmp_path, fake, UBER_MAX_FARE="XXD 75", UBER_MAX_FARE_24H="XXD 75")
    token = preview(svc)["confirm_token"]
    ctx = multiprocessing.get_context("spawn")
    barrier, results, sink = ctx.Barrier(2), ctx.Queue(), tmp_path / "synthetic-effects"
    child = ctx.Process(target=process_confirm, args=(str(tmp_path), token, barrier, results, str(sink), crash))
    child.start()
    barrier.wait(15)
    child.join(20)
    if child.is_alive():
        child.terminate()
        child.join()
    assert child.exitcode == exit_code
    assert (sink.read_bytes().count(b"effect\n") if sink.exists() else 0) == effects
    restart = make_service(tmp_path, fake, signed_in=False, **CAPS)
    before = len(fake.sent)
    if crash == "before_intent":
        assert restart.ledger.spent_24h("XXD") == 0
        assert restart.request_ride(*ARGS, token)["requested"]
    else:
        assert restart.ledger.spent_24h("XXD") == 5000
        assert code(restart.request_ride, *ARGS, token) == "outcome_unknown"
        assert len(fake.sent) == before


def test_legacy_state_preserved_unbound_previews_invalidated_and_new_schema_fails_closed(tmp_path):
    ledger = Ledger(tmp_path)
    now = ledger._clock()
    data = {
        "opaque": {"future": [1, 2]},
        "frozen": "legacy",
        "booked": [{"at": now, "cents": 100, "currency": "XXD", "trip": fx.TRIP}],
        "reservation_attempts": {"legacy": {"status": "unknown", "cents": 200, "currency": "XXD"}},
        "pending": {"rq_old": {"kind": "rq", "expires_at": now + 100}},
    }
    fsio.atomic_write_json(ledger.path, data)
    assert ledger.spent_24h("XXD") == 300
    assert ledger.pending("rq_old", "rq") is None
    assert ledger.frozen() == "legacy"
    ledger._change(lambda d: None)
    saved = json.loads(ledger.path.read_text())
    assert saved["opaque"] == data["opaque"] and saved["pending"] == {} and saved["frozen"] == "legacy"
    saved["action_schema"] = 2
    fsio.atomic_write_json(ledger.path, saved)
    with pytest.raises(UberError) as exc:
        ledger.actions()
    assert exc.value.code == "booking_state_invalid"


@pytest.mark.parametrize("broken", [None, [], {"effect": "unknown"}])
def test_malformed_action_records_fail_closed(booking_service, broken):
    svc = booking_service
    token = preview(svc)["confirm_token"]
    svc.request_ride(*ARGS, token)
    data = json.loads(svc.ledger.path.read_text())
    data["action_attempts"][token] = broken
    fsio.atomic_write_json(svc.ledger.path, data)
    assert code(svc.request_ride, *ARGS, token) == "booking_state_invalid"


def test_plain_reads_keep_their_existing_partial_data_taxonomy(service, fake):
    fake.override["GetStatus"] = (418, json.dumps({"data": fx.status(), "errors": [{"message": "partial"}]}))
    assert service.ride_status() is not None


def test_observation_helper_guards_all_stale_apply_paths(reserve):
    svc, args, raw = reserve
    receipt = svc.reserve_ride(confirm=svc.reserve_ride(**args)["confirm_token"])
    key = receipt["attempt_id"]
    stale = svc.reservation_details(RESERVATION)
    raw["reservationStatus"] = "FAILED"
    with svc.ledger.action():
        svc.booking._observe_target(RESERVATION, detail=svc.reservation_details(RESERVATION))
        svc.ledger.observe_reservation(key, RESERVATION, detail=stale)
        svc.ledger.observe_reservation(key, RESERVATION, error="upstream_error")
        svc.ledger.observe_reservation(key, RESERVATION, aged=True)
    state = svc.ledger.attempts()[key]
    assert state["status"] == "FAILED" and state["attention"] == "reservation_failed"
    assert not state.get("monitor_disabled")


def test_cancellation_resolution_retains_spend_and_never_fabricates_ack(reserve, fake):
    svc, args, raw = reserve
    svc.reserve_ride(confirm=svc.reserve_ride(**args)["confirm_token"])
    token = svc.cancel_reservation(RESERVATION, None)["confirm_token"]
    fake.override["TripCancel"] = lost
    assert code(svc.cancel_reservation, RESERVATION, token) == "outcome_unknown"
    raw["reservationStatus"] = "CANCELED"
    svc.booking.resolve_action(
        held(svc)["id"], target=RESERVATION, evidence="independent cancellation target observation"
    )
    assert held(svc)["effect"] == "cancelled" and held(svc)["acknowledged"] is False
    assert held(svc)["observed_cancelled"] and svc.ledger.spent_24h("XXD") == 5000
    before = len(fake.sent)
    assert svc.cancel_reservation(RESERVATION, token)["observed_cancelled"]
    assert len(fake.sent) == before and fake.ops().count("TripCancel") == 1


def test_proven_unsent_cancel_allows_only_fresh_approval(booking_service, fake, monkeypatch):
    svc = booking_service
    fake.trip = fx.live_trip()
    token = svc.cancel_ride(None)["confirm_token"]
    original = svc.ledger.begin_action

    def begin(*args, **kwargs):
        a = original(*args, **kwargs)
        monkeypatch.setattr(
            svc.uber.transport.state, "check_pause", lambda: (_ for _ in ()).throw(UberError("blocked"))
        )
        return a

    with monkeypatch.context() as m:
        m.setattr(svc.ledger, "begin_action", begin)
        assert code(svc.cancel_ride, token) == "blocked"
    monkeypatch.undo()
    assert held(svc)["effect"] == "not_submitted"
    assert svc.cancel_ride(svc.cancel_ride(None)["confirm_token"])["cancelled"]
    assert fake.ops().count("TripCancel") == 1


def test_hold_currency_and_exact_rolling_boundary_are_independent(booking_service, fake):
    svc = booking_service
    now = svc.ledger._clock()
    svc.ledger._clock = lambda: now
    token = preview(svc)["confirm_token"]
    fake.override["TripRequest"] = lost
    assert code(svc.request_ride, *ARGS, token) == "outcome_unknown"
    assert svc.ledger.spent_24h("USD") == 0
    for offset, expected in [(WINDOW - 0.001, 5000), (WINDOW, 0), (WINDOW + 0.001, 0)]:
        svc.ledger._clock = lambda offset=offset: now + offset
        assert svc.ledger.spent_24h("XXD") == expected
    assert len(svc.ledger.held_actions()) == 1


@pytest.mark.parametrize(
    "answer", [(200, "not json"), (403, "<!doctype html>challenge-platform"), (200, "[1,2]"), (200, "null")]
)
def test_post_open_body_and_challenge_failures_stay_unknown(booking_service, fake, answer):
    svc = booking_service
    token = preview(svc)["confirm_token"]
    fake.override["TripRequest"] = answer
    with pytest.raises(UberError) as exc:
        svc.request_ride(*ARGS, token)
    assert exc.value.code == "outcome_unknown" and set(exc.value.extra) == {"operation_id"}
    assert exc.value.extra["operation_id"] == held(svc)["id"]
    assert svc.ledger.spent_24h("XXD") == 5000


def test_local_queue_timeout_is_proven_before_open(booking_service, fake, monkeypatch):
    svc = booking_service
    token = preview(svc)["confirm_token"]
    transport = svc.uber.transport
    original = svc.ledger.begin_action

    def begin(*args, **kwargs):
        a = original(*args, **kwargs)
        assert transport._lock.acquire(blocking=False)
        transport._queue_wait = 0
        return a

    monkeypatch.setattr(svc.ledger, "begin_action", begin)
    try:
        assert code(svc.request_ride, *ARGS, token) == "busy"
    finally:
        transport._lock.release()
    assert "TripRequest" not in fake.ops() and svc.ledger.spent_24h("XXD") == 0
    assert held(svc)["evidence"]["source"] == "local_before_open"


@pytest.mark.parametrize(
    "status,errors",
    [(200, [{"path": ["tripRequest", "uuid"], "extensions": {"code": "INTERNAL_SERVER_ERROR"}}]), (418, [])],
)
def test_reserve_uses_same_narrow_envelope_refusal_predicate(reserve, fake, status, errors):
    svc, args, _ = reserve
    token = svc.reserve_ride(**args)["confirm_token"]
    fake.override["TripRequest"] = (
        status,
        json.dumps({"data": {"tripRequest": {"errorCode": "REFUSAL"}}, "errors": errors}),
    )
    assert code(svc.reserve_ride, None, None, None, None, None, None, token) == "outcome_unknown"
    assert svc.ledger.spent_24h("XXD") == 5000
    assert next(iter(svc.ledger.attempts().values()))["status"] == "unknown"


def test_different_cancel_target_is_independent(booking_service, fake):
    svc = booking_service
    fake.trip = fx.live_trip()
    first = svc.cancel_ride(None)["confirm_token"]
    assert svc.cancel_ride(first)["cancelled"]
    fake.trip = {**fx.live_trip(), "uuid": "00000000-0000-4000-8000-0000000000ff"}
    second = svc.cancel_ride(None)["confirm_token"]
    assert svc.cancel_ride(second)["trip_id"] == fake.trip["uuid"]
    assert fake.ops().count("TripCancel") == 2
    assert len(svc.ledger.actions()) == 2


def test_local_attempt_cli_is_scoped_and_requires_explicit_evidence(booking_service, fake, monkeypatch, capsys):
    from uber_mcp.cli import main

    svc = booking_service
    token = preview(svc)["confirm_token"]
    fake.override["TripRequest"] = lost
    assert code(svc.request_ride, *ARGS, token) == "outcome_unknown"
    operation = held(svc)["id"]
    monkeypatch.setenv("UBER_STATE_DIR", str(svc.settings.state_dir))
    before = len(fake.sent)
    svc.session.connection.disconnect(svc.session)
    assert main(["admin", "attempt", "list"]) == 0
    output = capsys.readouterr().out
    assert operation in output and token not in output and "Example Mall" not in output
    assert main(["admin", "attempt", "resolve", operation, "--not-submitted"]) == 2
    assert (
        main(
            [
                "admin",
                "attempt",
                "resolve",
                operation,
                "--not-submitted",
                "--evidence",
                "authoritative synthetic non-submission receipt",
            ]
        )
        == 0
    )
    assert svc.ledger.spent_24h("XXD") == 0 and len(fake.sent) == before


@pytest.mark.parametrize("change", ["taxi_ceiling", "currency"])
def test_fresh_quote_cannot_change_approved_currency_or_taxi_ceiling(booking_service, fake, change):
    svc = booking_service
    args = (ARGS[0], ARGS[1], "Meter Taxi" if change == "taxi_ceiling" else "UberX", None)
    token = svc.request_ride(*args, None)["confirm_token"]

    def products(_):
        data = fx.products({})
        for tier in data["products"]["tiers"]:
            for product in tier["products"]:
                if product["displayName"] == args[2]:
                    if change == "currency":
                        product["currencyCode"] = "USD"
                    else:
                        product["fares"][0].update(fare="XX$40-70", fareAmountE5=7000000)
        return {"data": data}

    fake.override["Products"] = products
    assert code(svc.request_ride, *args, token) in {"price_rose", "over_cap"}
    assert "TripRequest" not in fake.ops() and svc.ledger.spent_24h("XXD") == 0
