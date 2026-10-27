# Working on uber-mcp

Instructions for contributors and coding agents. `AGENTS.md` is the only agent instruction file.

**This repository is public.** Never commit personal data or anything about a particular deployment: no real names,
email addresses, phone numbers or account details, no recorded payloads from a real Uber account, no addresses,
places, coordinates, trips, fares, drivers, plates or payment methods, no host names, networks or infrastructure of
where it runs, and no anecdotes about who used it when. Deployment-specific configuration and runbooks belong with the
deployment, not here. This applies to code comments, docs, tests, fixtures and commit messages alike.

## Verify

`make bootstrap`, then `make check`: ruff, the format check, the lock-consistency check (`requirements-runtime.lock`
and `requirements-login.lock` must equal `uv export` of `uv.lock`; run `make lock` after a dependency change) and
pytest, with a source-bound receipt under `.artifacts/`. Documentation-only changes need `git diff --check` and link
checks.

Tests never reach Uber: `tests/conftest.py` puts a fake Uber behind the real `Transport`, answering each GraphQL
operation the way [docs/uber-web-api.md](docs/uber-web-api.md) describes. Fixtures are synthetic
(`tests/fixtures/README.md`): shaped like Uber's responses, with invented values.

## Boundaries

- `tests/test_safety.py` is the safety boundary: a new Uber host, a new GraphQL operation (read or money), a network
  import outside `uber.py` (and `cli.py`'s local health check), or a money operation named or
  sent anywhere but `booking.py` is a deliberate change there, never a side effect. `uber_request_ride`,
  `uber_cancel_ride`, `uber_reserve_ride` and `uber_cancel_reservation` are the only destructive tools; `uber_connect` (a sign-in link, nothing at Uber) is the only
  other tool not marked read-only.
- **Booking lives only in `booking.py`.** `TripRequest` and `TripCancel` go out only through
  `Uber.graphql(..., money=True)` from there, and only after a preview, a single-use confirm token bound to the
  preview's arguments, a fresh price no higher than the approved one, both caps, the freeze check and the
  one-ride-at-a-time check, with every attempt in `audit.jsonl`. Weakening any of these, or adding a way to book
  around them (scheduled rides, tips, other payment flows), is a design change: argue it in the pull request.
- Reserve uses native `TripRequest(type=RESERVE, pickupTimeMsec=...)`, immutable stored approvals, exact payment
  binding, market-returned terms and the same caps. `TripCancel(current=False, uuid=reservationUUID)` cancels it;
  its fee lookup uses the separate `jobUUID`. Never interchange those IDs. Native operations are documented in
  `docs/uber-web-api.md`; new money flows need explicit design and safety-test changes.
- An outcome that may have reached Uber is `outcome_unknown`, never retried by the server. Reserve attempts persist
  before submission and hold their authorization until a verified result or explicit operator resolution. Monetary
  actions serialize per account across processes. Background reservation monitoring is read-only.
- `tests/test_login_safety.py`: the sign-in window shows only Uber's pages (and Google's or Apple's sign-in) and
  automates nothing on them: no click, keystroke, fill or form call exists in `src/uber_login`. The login service's
  API needs its bearer and the person's view needs its per-sign-in token.
- Sign-in links (`connect.py`, `tests/test_connect.py`): a link is made only for the caller's own account, works once
  and for a short time, and can only put a session into that account (never show, return or read one); a session for
  a different rider than the account already has, or one another account holds, is refused. The links, their page,
  the live view, the paste add-on and the login service's windows come from
  [signin-window](https://github.com/benjamin-di/signin-window): fix them there, not here; this repository holds
  Uber's window (`uber_login/window.py`) and what a sign-in does to an account (`connect.py`).
- Hosted browser routes require owner OIDC authentication through the shared `signin_window.oidc` integration.
  `connections.py` owns signed-identity account mapping; `connection_state.py` owns persistent owner/rider bindings
  and disconnect barriers. Never fall back to capability-only links. Keep account controls independent of browser
  configuration and preserve them on rollback. A reconnect is verified in staging before replacing accepted
  credentials. Disconnect never removes booking history, cancels a ride or resets budgets. OIDC control-plane
  traffic is confined to the configured issuer and never widens Uber's data-plane host or operation allowlist.
- Tool results never carry exception text or cookies; errors use the fixed `NEXT_STEP` table in `errors.py`. Logs
  never carry tool arguments (places), cookie values or Uber's error text.
- `operations.json` holds Uber's own GraphQL documents, refreshed with `scripts/sync-operations.py`; never hand-write
  a document Uber's web apps do not send.
- Live checks against Uber: only your own account, a handful of paced read-only calls, nothing they return is
  committed. Never request or cancel a ride in an automated check or experiment without explicit owner authorization of the
  booking and cleanup scope; verify current charges and cancellation terms first. Never use someone else's account.

## Releases

A `vX.Y.Z` tag whose version matches `pyproject.toml` runs `.github/workflows/release.yml`: the tests, then both
images (smoke-tested, pushed to GHCR, promoted from an immutable `:sha-<12>` candidate) and a GitHub release.
