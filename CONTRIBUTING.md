# Contributing

Thanks for helping. Issues and pull requests are welcome, especially when Uber changes its web API and a tool starts
failing with `schema_changed`.

## Setup

```sh
git clone https://github.com/benjamin-di/uber-mcp-server
cd uber-mcp-server
make bootstrap   # uv sync --locked
make check       # lint, format, lock consistency, tests
```

`uv run uber-mcp --help` runs your checkout. To try it against Uber, sign in with
`uv run --extra login uber-mcp login` and use a separate `UBER_STATE_DIR` for development. Leave `UBER_MAX_FARE` and
`UBER_MAX_FARE_24H` unset while developing: booking then stays off.

When Uber renames an operation or a field, `uv run python scripts/sync-operations.py` reads the current GraphQL
documents from Uber's own web apps (with the signed-in session of `UBER_STATE_DIR`) and rewrites
`src/uber_mcp/operations.json`; it names any operation it can no longer find.

## Rules

- **No personal data.** Never commit real account data, recorded payloads from a signed-in session, cookies, names,
  email addresses, phone numbers, places, coordinates, trips, fares or payment details, in code, tests, fixtures,
  docs or commit messages. Fixtures are synthetic (`tests/fixtures/README.md`).
- **Tests never contact Uber.** Model new behaviour in the fakes (`tests/conftest.py`, `tests/fixtures/uber.py`) and
  document what you observed in [docs/uber-web-api.md](docs/uber-web-api.md).
- **The safety boundary is deliberate.** Adding an Uber host or GraphQL operation, or anything that could book,
  cancel or pay outside `src/uber_mcp/booking.py` and its preview, confirm and cap checks, means changing
  `tests/test_safety.py` on purpose and explaining why in the pull request.
- Keep tool outputs stable: fields may be added, not renamed or removed, without a major version.
- Be gentle with Uber when testing live: only your own account, a handful of read-only requests, and commit nothing
  they return. Never request or cancel a ride to test something.

AGENTS.md has the same rules for coding agents.

## How changes land

Each release of this repository is exported from a private working repository as one
`Release X.Y.Z` commit, so pull requests are not merged here: the next release would replace
them. When a change fits, the maintainer applies it there, credits you in the release notes
and closes the pull request with a link to the release that contains it. Issues are welcome;
answers are best effort.
