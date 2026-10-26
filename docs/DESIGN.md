# Design record

Why the server looks the way it does. [README.md](../README.md) is the contract for users;
[uber-web-api.md](uber-web-api.md) holds the evidence about Uber's endpoints. This file keeps the decisions and the
reasons behind them.

## Architecture

| Module | Role |
|---|---|
| `server.py` | The FastMCP server: tools, instructions, error mapping, the keep-alive thread, status and canary. |
| `uber.py` | The only network I/O to Uber: `Transport` (two hosts, no redirects, one request at a time, pacing, budget, pause) and `Uber.graphql` (allowlisted operations, headers, answer classification, cookie write-back). |
| `session.py` | The one session file: generations, sealing, cookie write-back, the signed-in verdict. |
| `booking.py` | The only sender of `TripRequest` and `TripCancel`: previews, confirm tokens, the price ceiling, caps, freeze and the audit log (`Ledger`). |
| `places.py`, `rides.py`, `trips.py`, `account.py` | Turning tool arguments into Uber's inputs and Uber's answers into tool results. |
| `state.py`, `fsio.py` | State shared by processes: budget, pause, unresolved errors, canary results; atomic writes and file locks. |
| `uber_login/` | The sign-in window (`window.py`, also used by `uber-mcp login`) and the login service for servers (`app.py`: [signin-window](https://github.com/benjamin-di/signin-window)'s service running Uber's window). |
| `connect.py` | Sign-in links (`uber_connect`) on signin-window's relay (the page, its calls, noVNC's files, the live view and the paste add-on): Uber's wording, and what a proven sign-in does to the link's account. |

## Decisions

| Decision | Why |
|---|---|
| An MCP server over Uber's rider web GraphQL, plain HTTP, no browser at runtime | Uber offers no API for riders to book their own rides. A browser agent is slow, token-heavy and unpredictable with money; plain HTTP with a signed-in session's cookies prices, books and tracks rides in a few calls. |
| Uber's own GraphQL documents (`operations.json`), refreshed from Uber's web bundles by `scripts/sync-operations.py` | The server then sends exactly what Uber's web app sends; a renamed field shows up as a GraphQL validation error (`schema_changed`), and one script run catches up. Hand-written documents drift and stand out. |
| One transport module, two hosts, an allowlist of operations, redirects refused | The boundary is checkable in one place and pinned by `tests/test_safety.py`. GraphQL answers never redirect: a redirect to `auth.uber.com` means signed out, any other means the API moved. |
| Money operations need `money=True`, and only `booking.py` passes it | A read path can never send `TripRequest` or `TripCancel` by mistake; a test walks the code for both names and the flag. |
| Booking is off unless both `UBER_MAX_FARE` and `UBER_MAX_FARE_24H` are set; one alone refuses to start | How much money the server may spend is the operator's decision, made before any model sees a tool. One cap without the other is a configuration mistake, not a policy. |
| Preview first, then a single-use confirm token bound to the preview's arguments (10 minutes) | A ride sends a car, cannot be undone (cancellations can cost a fee) and spends money, so the person's yes must refer to one exact preview. The token stops a model from confirming a different trip, option or payment method than the one shown, and from confirming twice. |
| At confirm, a fresh quote for the same option; never above the approved price, a fresh preview when it rose | Uber's upfront price is signed for about four minutes, while a person may answer later. Booking at the fresh price, only when it is not higher, keeps the yes truthful; a lower price books. |
| For a metered taxi, the approved price is the range's high end (Uber's `fareAmountE5`) | The meter decides the fare; the high end is Uber's own ceiling and the only number a cap can be checked against. |
| Caps are checked at preview and at confirm, in the caps' currency; another currency is `over_cap` | A preview that could never be booked is useless to show; prices move between the two calls. Comparing amounts across currencies would need exchange rates the server should not guess. |
| The 24-hour spend is a local ledger (`booking.json`) of rides booked through this server, cancelled ones included | It is the server's own spending that the cap limits; a cancellation can still cost a fee, and keeping the approved price is the safe side. |
| One ride at a time (`GetStatus` before a preview and before a confirm) | A second request while a car is coming is almost always a mistake, and a confirm must not book on top of a ride that started meanwhile. |
| A lost answer to a money call is `outcome_unknown`, never retried | `TripRequest` is not idempotent: a retry can send two cars. The assistant reads `uber_ride_status` instead. Failures that prove nothing was sent (budget, pause, busy, no session) keep their own codes. |
| Native Reserve uses a distinct preview/confirmation tool | Future quotes, pickup-local wall time, reservation terms and durable uncertainty need checks beyond on-demand booking. Approval confirms only the stored proposal. |
| A freeze switch in the ledger, set only by `uber-mcp admin freeze`; cancelling still works | An operator's stop that needs no restart, that no tool can lift, and that drops every pending preview. Stopping a ride must stay possible when booking is stopped. |
| An append-only `audit.jsonl` of every money attempt, without places | Who booked what at which price must be answerable afterwards, without keeping a location history. |
| One caller account per operation, selected only by the trusted proxy | No tool argument names an account; sessions, approvals and reservation attempts stay within the account's state directory. Several backend bearers exist only to rotate them. |
| One session file, held in memory, re-read when it changes (inode, mtime, size) | Each MCP client on a computer starts its own server process; a sign-in in any of them, or by `uber-mcp login`, reaches all of them with their next call. |
| Each sign-in raises the generation; renewed cookies are written back under a lock to the same generation only | Uber renews `jwt-session` (about a day) on reads, so the file must follow or the next process starts stale. A write-back from an older process never overwrites a newer sign-in. |
| Sealed with AES-GCM under `UBER_SESSION_KEY` (`session.bin`), otherwise `session.json` readable only by its owner | On a server the state volume may be backed up or copied; the key lives elsewhere. On a person's own computer a file mode is the usual protection and needs no key to manage. |
| A session is `unverified` until a read as the rider proves it; Uber refusing it makes it `dead` until a new sign-in | `uber_status` reports what Uber last said, not what the file claims. A read that shows another rider than the one signed in is `session_invalid`. |
| A keep-alive thread reads both of Uber's sites every 4 hours while signed in | It renews the short-lived cookies on both surfaces and finds an ended session before a person needs a ride. It waits 15 minutes after a failure and never runs before the first sign-in. |
| A cached canary in `uber_status(check=true)`: hourly the keep-alive reads, daily a place search and the prices of a short trip near Uber's guess of the rider's position | Monitoring calls it often; the cache keeps that to a few Uber requests a day. The deep check covers the operations a booking needs without booking anything; results are per session generation. |
| Pacing: one request at a time, about one a second, a daily budget (UTC), a pause of 5 minutes, 30 when pushback repeats within two hours | Gentle use of an unofficial API. A single hiccup costs minutes, real blocking meets the long pause on its second sign. A challenge page pauses without ending the session. |
| The person signs in in a real browser window; nothing on the sign-in is automated | Uber signs in with one-time codes, Google, Apple or a QR code, behind its own bot checks. A person passes them; the server keeps the cookies. `tests/test_login_safety.py` pins that no click, keystroke or fill exists. |
| The window shows only Uber's pages (and Google's or Apple's sign-in); after landing it opens Uber's trips site once and reads both | `riders.uber.com` signs in through Uber's single sign-on on first visit and has its own cookies; proving both before saving means a saved session works for every tool. |
| On a server, a login service beside it: a virtual display, a remote-desktop server on loopback and noVNC, per sign-in | A person can sign in from any browser, including a phone, without the server ever seeing a password. The service's API needs a bearer, each sign-in's view and stream need their own token, a proven session is handed over once (and dropped after 5 minutes), and the browser profile is deleted. The service holds no key and no session; the server seals what it receives. |
| Sign-in links served by the server itself, not by the login service | The server knows who is calling (the trusted account header), so it can bind a link to the caller's account and store the session there; the login service stays private with its bearer. The id is the only credential on the page, so it is single-use, short-lived and rate-limited, and it can only put a session into its account, never read one. |
| Paste by typing the clipboard's text into the window, from a small module beside noVNC's stock viewer | noVNC 1.6 swallows Ctrl+V on its canvas and has no clipboard API support; letting the browser's own paste event fire and typing the text key by key (as noVNC's on-screen keyboard does) needs no change to noVNC and no clipboard permission. `scripts/paste_e2e.py` checks it in real browsers in the release workflow. |
| Places as saved labels, Google place ids, `lat,lng` or free text echoed back with alternatives | Agents mostly know names; Uber's search uses Google place ids, so ids from a Google Maps tool work too. A free-text match is shown, never trusted silently. A `lat,lng` keeps the exact point asked for, so an airport terminal's point sets the rider down there. |
| Short caches: the account and saved places 10 minutes, Uber's guess of the rider's position an hour | Pricing and booking read the payment methods on every call; they change rarely. |
| Errors from a fixed `NEXT_STEP` table; booking decisions (`over_cap`, `price_rose`, `trip_active`, ...) are not counted as failures | Exception text can echo cookies or places. A cap doing its job is not a service problem, so `last_error` ignores it. |
| stdio by default, HTTP only behind a bearer | A client on the same computer starts the server and owns it; over a network the bearer is the only gate, so the server refuses HTTP without one. |
| Open output schemas | A closed schema breaks cached MCP clients when a field is added. |
| Several accounts: the default in the state folder, others in `accounts/<id>/`; the caller's account from a header the operator's authenticating proxy sets, read only with the server's bearer | A household shares one server and one home connection to Uber, so pacing, budget and pause stay shared while sessions, caps, ledgers and confirm tokens stay per account. The server has no identity of its own to check; it trusts the proxy that already authenticated the person, which is why only that proxy may hold the bearer. An unknown account fails closed (`account_not_connected`) and never falls back to the default, and a new account cannot book until it is given caps. |

## Not tested, and why

- Automated tests never request or cancel real rides. They pin the protocol against synthetic fakes. Native Reserve
  has also had an explicitly authorized creation/retrieval/no-charge-cancellation acceptance; this does not qualify
  every product or market, nor driver assignment, dispatch or journey completion.
- Rate limits under bursts: probing them would risk the account.
- Prompt injection through place names, driver names or trip descriptions: content cannot be planted. Mitigated by
  the instructions, by treating Uber's text as data, and by money tools that act only on a token the person approved.

## Risks

- Unofficial endpoints change: the canary covers the account, the live status, place search and prices, and the
  fixtures pin the shapes the code reads. A change shows up as `schema_changed`.
- Uber's terms: automated use of a rider account may not be acceptable to Uber, and the account may be challenged or
  restricted. The server is gentle by design; the risk stays with the person using it.
- Session lifetime is Uber's decision; an ended session is reported as `session_expired`.
- Bot defence: a challenge page pauses the server; a person may need to sign in again.
- The 24-hour cap counts only rides booked through this server, not those booked in the Uber app.

## Research basis

Design ideas came from [crafter-station/uber-cli](https://github.com/crafter-station/uber-cli) and
[AriOliv/uber-mcp](https://github.com/AriOliv/uber-mcp), both MIT-licensed. Taken from uber-cli: talking to the rider
web GraphQL endpoints with a signed-in session, the variables of the price and ride requests, and reading Uber's own
documents from its web bundles instead of writing them by hand (`scripts/sync-operations.py` is a port of its sync
script; [THIRD_PARTY_NOTICES](../THIRD_PARTY_NOTICES)). Built here: the preview and confirm token, the
price ceiling at confirm, the caps and the freeze, the audit log, sealed sessions with write-back, and a sign-in that
never automates the login.
