# uber-mcp

An [MCP](https://modelcontextprotocol.io) server for Uber rides. It lets an AI assistant (Claude, ChatGPT, Cursor,
...) find places, compare every ride option with Uber's live prices, request a ride once you have said yes to the
exact price, follow the live ride (driver, car, plate, PIN, arrival time), cancel it, and read your trip history.

> Unofficial. Not affiliated with or endorsed by Uber Technologies, Inc. Uber has no public API for booking rides as
> a rider; this server uses the same endpoints Uber's own rider website uses (`m.uber.com` and `riders.uber.com`),
> signed in as you, with the session of a browser you signed in to yourself. Uber can change those endpoints at any
> time, and the server then stops working until it is updated. Use it with your own account, for your own rides;
> Uber's terms of use apply to you, and whether automated use of your account is acceptable to Uber is your risk.

**Requesting a ride spends your money and sends a real car.** The server never does that on one call: every request
is a preview first and a single-use confirm token second, within price caps you set, and booking stays off until you
set them ([Booking safety](#booking-safety)).

**Requirements**

- An Uber rider account with a payment method, in a city where Uber's rider website (`m.uber.com`) offers rides.
- [uv](https://docs.astral.sh/uv/) (it fetches Python 3.12 if needed), or Docker for a server.
- Google Chrome for signing in (or Chromium, see [Signing in](#signing-in)).

## Quick start

1. **Sign in once.** A browser window opens on Uber's own sign-in page; sign in as usual (a code, Google, Apple or a
   QR code). It closes once you are signed in, and the session is saved for the server.

   ```sh
   uvx --from "uber-mcp[login]" uber-mcp login
   ```

2. **Add the server to your client.** It runs over stdio, started by the client. As configured here it can look up
   places, prices, rides and trips but cannot book: booking stays off until step 3.

   - Claude Code:

     ```sh
     claude mcp add uber -- uvx uber-mcp
     ```

   - Claude Desktop, Cursor and most other clients: add to the client's MCP configuration

     ```json
     {
       "mcpServers": {
         "uber": {
           "command": "uvx",
           "args": ["uber-mcp"]
         }
       }
     }
     ```

3. **To let it request rides**, set both caps in the server's environment, in your account's currency: the most one
   ride may cost and the most everything it books may cost in any 24 hours, for example
   `claude mcp add uber -e UBER_MAX_FARE="USD 80" -e UBER_MAX_FARE_24H="USD 200" -- uvx ...`, or
   `"env": { "UBER_MAX_FARE": "USD 80", "UBER_MAX_FARE_24H": "USD 200" }` beside `"args"` ([Booking
   safety](#booking-safety)).

4. **Ask.** "How much is an Uber from home to the airport right now?", "Book the cheapest one", "Where is my
   driver?", "What did my last three rides cost?".

These commands run the latest release from PyPI ([`uber-mcp`](https://pypi.org/project/uber-mcp/)); write
`uber-mcp==X.Y.Z` to stay on a release, or `--from git+https://github.com/benjamin-di/uber-mcp-server` for the latest code.

## Tools

| Tool | What it does |
|---|---|
| `uber_places(query, near)` | No query: your saved places (Home and any you named, with their labels). With a query: Uber's place search around `near` (`lat,lng` or a saved place's label), else around Uber's guess of your position. Each place has an `id` (a Google place id), a name, an address and, for airports, the terminals per airline. |
| `uber_ride_options(pickup, dropoff, payment)` | Every ride option Uber offers for the trip right now: name, tier, seats, live `price`, pickup ETA, arrival time and badges, with the pickup and drop-off as Uber matched them (`*_matched_by`, and `*_alternatives` for a free-text match), `quote_expires_at`, the payment method and the server's booking caps. Read-only. |
| `uber_request_ride(pickup, dropoff, option, payment, confirm)` | Without `confirm`: a preview (the ride, a fresh price, the places, the payment method, the caps) and a `confirm_token`; nothing is booked. With `confirm` (after the person's yes, same arguments): books it, unless the price rose or a cap would be passed. |
| `uber_ride_status()` | The current ride: status and Uber's headline, driver (name, rating), car and plate, PIN, pickup ETA, arrival time, fare, whether it can still be cancelled, and the stops. Only `state` when there is none. |
| `uber_cancel_ride(confirm)` | Without `confirm`: the ride and Uber's cancellation notice (any fee) with a `confirm_token`; nothing is cancelled. With `confirm`: cancels that ride. |
| `uber_trips(limit 1..20, cursor)` | Upcoming (on the first page) and past trips, newest first: id, where to, when, fare and Uber's page for it. Pages back with `next_cursor`. |
| `uber_trip(id)` | One trip: status, fare, product, driver, pickup, drop-off and stops, times, distance, duration, surge, scheduled, the rating you gave and Uber's route map image. |
| `uber_account()` | The signed-in rider: first name, rating, sign-up country, completed trips, Uber One, payment methods (id, name, type, which one the app uses), Uber Cash, money owed, and the server's booking caps and 24-hour spend. |
| `uber_status(check)` | Sign-in state (`signed_in`, `generation`, when proven, `cookie_expiry`), today's request budget, any pause, booking caps and spend, `last_error`, and `sign_in_links` (whether `uber_connect` works here). `check=true` adds the cached canary (for monitoring). |
| `uber_connect()` | A one-time link where the person signs in to their own Uber account, for the caller's account only ([Signing in with a link](#signing-in-with-a-link)). Not read-only (it makes the link), never destructive. |

A pickup or drop-off can be a saved place's label (`Home`), a place id from `uber_places` (Google place ids such as
`ChIJ...`, so ids from a Google Maps tool work too), `lat,lng`, or free text (Uber's top match, echoed back with
alternatives: check it). Airports list their terminals; pass a terminal's `lat,lng` to be set down there.

A price is `{"display": "...", "cents": 4250, "currency": "USD", "kind": "fixed"}` with Uber's own display string:

- `fixed`: Uber's upfront price, held for a few minutes (`quote_expires_at`).
- `range`: a metered taxi. The meter decides the fare; `cents` is the range's high end and `low_cents` the low end.
- `estimate`: neither of the above.

Options marked `reserve_only` can only be booked ahead, in the Uber app. Caps and spend are
`{"cents": 8000, "currency": "USD", "display": "USD 80.00"}`. Errors are `{"error": {code, next_step, retryable}}`,
and the `next_step` text tells the assistant what to do ([Troubleshooting](#troubleshooting)).

## Booking safety

Enforced in code (`src/uber_mcp/booking.py`, the only module that can send a ride request or cancellation) and pinned
by `tests/test_safety.py` and `tests/test_booking.py`:

- **Off unless you set both caps.** Without `UBER_MAX_FARE` (one ride) and `UBER_MAX_FARE_24H` (everything booked
  through this server in any 24 hours), `uber_request_ride` answers `booking_disabled` before asking Uber anything.
  Both use the same currency, and the per-ride cap cannot be above the 24-hour one; one without the other refuses to
  start.
- **Preview, then confirm.** The first call only previews and returns a `confirm_token`. The token works once, for
  10 minutes, and only with exactly the arguments of its preview. The tool descriptions and the server's instructions
  tell the assistant to show the preview and wait for the person's explicit yes; the tools are marked destructive, so
  clients that ask before such calls do.
- **Never above the approved price.** At confirm the server asks Uber for a fresh price for the same option. If it is
  higher, or in another currency, nothing is booked and a fresh preview comes back (`price_rose`) for the person to
  approve. A lower price books. For a metered taxi the approved price is the range's high end; the meter decides.
  For an `estimate` Uber has not committed to a price, so the final fare can differ from the approved one.
- **Within the caps.** The ride's price must fit the per-ride cap and, together with the rides this server booked in
  the last 24 hours, the 24-hour cap, in the caps' currency (`over_cap`, with the reason). Cancelled rides still
  count; rides booked in the Uber app do not.
- **One ride at a time.** A preview or a confirm while a ride is active answers `trip_active`.
- **No blind retries.** When a request was sent but its answer is lost, the result is `outcome_unknown`: the
  assistant reads `uber_ride_status`, and the server never repeats a request on its own.
- **Freeze.** `uber-mcp admin freeze [REASON]` stops all booking at once and drops every pending preview;
  `uber-mcp admin unfreeze` resumes within the caps. Cancelling still works while frozen.
- **Audit log.** Every confirm, booked or not (refused with the reason, started, failed, refused by Uber, succeeded;
  cancels too), is appended to `audit.jsonl` with the product, the price and the trip id, never the places.
  `uber-mcp admin audit [-n N]` shows the latest.
- **Only booking.** No endpoint for payment methods, tips, ratings, profiles, saved places, scheduled rides, Uber
  Eats or account settings exists in the code.

## Other safety

- **Only Uber.** Requests go only to `m.uber.com` and `riders.uber.com`, and only with the GraphQL operations listed
  in `src/uber_mcp/uber.py`; a redirect is never followed.
- **Gentle.** One request at a time, about one a second, a daily request budget, and a pause of 5 minutes (30 when it
  repeats within two hours) whenever Uber pushes back.
- **Your account only.** The server acts for the signed-in rider (with [several accounts](#accounts), for the
  account the request is for, never another). Every read of the account checks that it is still the rider who signed
  in. Place names, driver names and trip descriptions are treated as data, not
  instructions.

## Privacy

Everything stays on the computer that runs the server, in its state folder (`~/.local/state/uber-mcp` on Linux,
`~/Library/Application Support/uber-mcp` on macOS, `%LOCALAPPDATA%\uber-mcp` on Windows, `/state` in the container;
`UBER_STATE_DIR` overrides it):

| File | What it holds |
|---|---|
| `session.json` (or `session.bin`) | Your Uber sign-in (cookies), the browser identity it was made with, your account id and first name. **Treat it like a password.** With `UBER_SESSION_KEY` it is `session.bin`, sealed with AES-GCM. |
| `browser/` | The sign-in window's browser profile (`uber-mcp login` only), so Uber recognises the browser next time. |
| `state.json` | Today's request budget, any pause, unresolved errors, keep-alive and canary results. |
| `booking.json` | Pending previews (for 10 minutes), the rides booked in the last 24 hours (price and trip id) and the freeze. |
| `audit.jsonl` | Every booking and cancellation attempt ([above](#booking-safety)). |
| `accounts/<id>/` | Each further account ([Accounts](#accounts)): its own `session.bin`, `state.json` (its errors, keep-alive and canary), `booking.json`, `audit.jsonl`, and `account.json` with its caps. |

Nothing is sent anywhere except Uber. What a tool returns (places, prices, the driver's name, the plate, the PIN)
goes to your assistant, like anything else you ask it. Logs (stderr) have one line per tool call with its name,
outcome and duration, never its arguments, cookies or Uber's error text. The account read includes your email
address and phone number (Uber's own query asks for them); the server drops them. `uber-mcp status` shows only the
first letter of the account's name. `uber-mcp logout` deletes the session and the browser profile.

## Signing in

`uber-mcp login` opens Uber's sign-in page in your installed Google Chrome, using a separate profile in the state
folder. You do every step yourself; nothing is clicked or typed for you, and the window shows only Uber's pages (and
Google's or Apple's, for their sign-in buttons). Once Uber shows its rides page, the window opens Uber's trips site
once (it signs in there through Uber's own single sign-on), reads your account on both with the same cookies, saves
the session and closes. A running server picks up the new session with its next call.

- No Chrome? Run `uvx --from "uber-mcp[login]" patchright
  install chromium`, then `uber-mcp login --browser chromium`.
- Uber decides how long a session lasts. The server reads both of Uber's sites every 4 hours
  (`UBER_KEEPALIVE_SECONDS`), which lets Uber renew its short-lived cookies; `uber_status` shows when each sign-in
  cookie expires. When the session ends, tools return `session_expired`: sign in again.
- `uber-mcp status` shows whether the saved session is signed in, without asking Uber anything.
- Every sign-in raises the session's `generation`; a newer sign-in always wins over cookies another process is still
  writing back.

### On a server: the login service

A server has no screen for the sign-in window. The login browser image (`ghcr.io/benjamin-di/uber-mcp-login-browser`)
runs Uber's sign-in page in a Chromium next to the server and shows it to the person through
[noVNC](https://github.com/novnc/noVNC) in their own browser. Nothing on the sign-in is automated there either.

1. Run the login service beside the server on a private network, with the same `UBER_LOGIN_TOKEN` (32 or more
   characters) on both, and `UBER_LOGIN_URL` on the server ([Running it as a server](#running-it-as-a-server)).
2. Start a sign-in on the server and wait:

   ```sh
   docker exec -it uber-mcp uber-mcp admin login            # --layout phone for a phone-sized window
   ```

   It prints a link, `UBER_LOGIN_VIEW_URL/view/<id>?t=<token>`, that works for this one sign-in only.
3. The person opens the link and signs in to Uber on the page it shows. The service checks the session on both of
   Uber's sites, hands it to the server once and closes the browser; the server seals it (`UBER_SESSION_KEY`) and
   saves it. `admin login` gives up after 20 minutes (`--minutes`).

The link must reach the login service's port 8080. The default, `http://localhost:8766`, suits the compose file
below (the service on the server's port 8766, loopback only) opened through an SSH port forward
(`ssh -L 8766:127.0.0.1:8766 <server>`); set `UBER_LOGIN_VIEW_URL` to your own HTTPS address if you publish the
service through a reverse proxy instead. Its API needs the bearer; the view page and the remote-desktop
stream need the per-sign-in token.

### Signing in with a link

With the login service and a public address (`UBER_PUBLIC_URL`, the address your reverse proxy serves the server at,
e.g. `https://example.com/uber` when `/mcp` is `https://example.com/uber/mcp`), people sign in without the operator:
they ask their assistant to connect Uber, the assistant calls `uber_connect` (the `not_signed_in` and
`session_expired` errors point it there) and gives them the link, and they sign in on Uber's own page in their
browser, phone or computer. The page names the account it connects and opens Uber's sign-in only when the person
continues (so a chat app's link preview starts nothing).

- **For the caller's account only.** The link is made for the account the request is for (the default account, or
  the one the trusted header names); there is no way to ask for another account's link. A newer link replaces the
  older one; at most 6 are made per account an hour.
- **One sign-in, 20 minutes.** `<UBER_PUBLIC_URL>/connect/<id>` (192 random bits) works until a sign-in succeeds or
  20 minutes pass, whichever is first; a reload or "try again" opens a new window, up to 4 times. Its pages are never
  cached (`Cache-Control: no-store`) and send no referrer. Requests for ids that do not exist are counted per address
  (from `X-Forwarded-For`, which the proxy must set): after 20 in ten minutes that address gets 429 instead of 404
  (a real link keeps working).
- **It only puts a session in.** Once the login service has proven the sign-in, the server stores it as that
  account's session and proves it once more with Uber; the page shows only the first letter of the rider's name.
  Nothing under `/connect` shows or returns a session. A sign-in as a different Uber rider than the account already
  has (even an expired one) is refused, and so is a rider another account on this server holds; the operator can
  still sign an account in with `uber-mcp admin login`.
- **Your proxy decides who can reach it.** `/connect/...` (the page, its calls, noVNC's files and the live view's
  WebSocket) is served without the bearer, because people open it in a browser; the link id is its credential. Route
  only `<prefix>/connect/...` to the server without authentication, never anything else, and do not add your
  bearer to it.
- **Pasting works.** Ctrl+V (Cmd+V) in the window types the clipboard's text into Uber's page (a small add-on to
  noVNC's viewer from [signin-window](https://github.com/benjamin-di/signin-window), the library the sign-in window
  and its links are built on; up to 1000 characters, characters the window's keyboard has, which covers ASCII). On a phone, noVNC's keyboard button opens the phone's keyboard, which pastes as usual.
- Passkeys do not work in a remote window: sign in with a code or a password.

Without the login service, sign in with `uber-mcp login` on a computer with a screen and copy its `session.json` into
the server's state folder; the server adopts it on its next call. A server with `UBER_SESSION_KEY` reads only
`session.bin`: sign in with the same key set (`UBER_SESSION_KEY=... UBER_STATE_DIR=... uber-mcp login`) and copy that.

## Running it as a server

For remote clients (claude.ai, ChatGPT and the Claude and ChatGPT apps reach connectors over HTTPS), run it over
streamable HTTP. It refuses to start without a bearer token:

```sh
docker run -d --name uber-mcp -p 127.0.0.1:8000:8000 --read-only --tmpfs /tmp \
  -e MCP_BEARER_TOKEN="$(openssl rand -hex 32)" \
  -e UBER_SESSION_KEY="$(openssl rand -base64 32)" \
  -v uber-state:/state \
  ghcr.io/benjamin-di/uber-mcp-server:latest
```

Like this it cannot book; add `-e UBER_MAX_FARE=... -e UBER_MAX_FARE_24H=...` to let it
([Booking safety](#booking-safety)). Keep the bearer and the session key: a new key cannot read a session sealed with
the old one (`session_invalid`, sign in again). Or run `MCP_BEARER_TOKEN=... uvx uber-mcp
serve --host 127.0.0.1 --port 8000`. The MCP endpoint is `/mcp`; `/health` is open for health checks (the image's
`HEALTHCHECK` runs `uber-mcp health`). Put it behind your own HTTPS reverse proxy to reach it from outside, and give
clients the bearer (`Authorization: Bearer <token>`).

The server image (amd64 and arm64) runs as uid 1000 on a read-only root filesystem: it writes only to `/state`, which
holds everything in [Privacy](#privacy), and to a `/tmp` tmpfs. With the login service, as a compose file:

```yaml
services:
  uber-mcp:
    image: ghcr.io/benjamin-di/uber-mcp-server:latest
    read_only: true
    tmpfs: [/tmp]
    ports: ["127.0.0.1:8000:8000"]
    volumes: [uber-state:/state]
    environment:
      MCP_BEARER_TOKEN: ${MCP_BEARER_TOKEN}
      UBER_SESSION_KEY: ${UBER_SESSION_KEY}
      # To let it book: both caps, in your account's currency (Booking safety).
      # UBER_MAX_FARE: USD 80
      # UBER_MAX_FARE_24H: USD 200
      UBER_LOGIN_URL: http://uber-mcp-login:8080
      UBER_LOGIN_TOKEN: ${UBER_LOGIN_TOKEN}
  uber-mcp-login:
    image: ghcr.io/benjamin-di/uber-mcp-login-browser:latest
    ports: ["127.0.0.1:8766:8080"]
    environment:
      UBER_LOGIN_TOKEN: ${UBER_LOGIN_TOKEN}
volumes:
  uber-state:
```

The login image (amd64) runs as uid 1000, keeps nothing (each sign-in gets a throwaway browser profile that is
deleted when it ends) and refuses to start without `UBER_LOGIN_TOKEN`. It can stay stopped between sign-ins.

## Accounts

One server can keep several Uber accounts apart, for example one per member of a household, behind an
authenticating reverse proxy that knows who is calling. Each account has its own sealed session, booking caps,
ledger, audit log and errors; Uber's pacing, the daily request budget and the pause after pushback are shared,
because Uber sees one client.

- The **default account** is the server's own state folder, with the caps from `UBER_MAX_FARE`/`UBER_MAX_FARE_24H`:
  a single-account server needs nothing new, and its existing session and ledger keep working. Name it with
  `UBER_DEFAULT_ACCOUNT` (default `default`) if the proxy sends that name for it.
- **Further accounts** live in `accounts/<id>/` (ids are 1-32 lower-case letters, digits or dashes). A new account
  has no caps, so it can look but not book until the operator sets them.
- **Which account a request is for** comes from a header the proxy sets, named by `UBER_TRUSTED_ACCOUNT_HEADER`
  (unset: every request is the default account's). It is read only on requests that carry the server's bearer, so
  **only the proxy may hold `MCP_BEARER_TOKEN`**, and the proxy must remove any copy of that header a client sends.
  A request without the header is the default account's; one whose header names an account that is not set up (or
  names it twice, or malformed) gets `account_not_connected` and is never served from another account. Over stdio
  there is no header: always the default account.
- Confirm tokens belong to the account that previewed them: a token from one account is `confirm_invalid` on any
  other.

```sh
docker exec uber-mcp uber-mcp admin account add alex          # read-only for now
docker exec -it uber-mcp uber-mcp admin login --account alex  # or the person asks their assistant (uber_connect)
docker exec uber-mcp uber-mcp admin account caps alex --max-fare "USD 40" --max-fare-24h "USD 100"
docker exec uber-mcp uber-mcp admin account list              # signed in or not, and the caps
docker exec uber-mcp uber-mcp admin account remove alex --yes # deletes its session, ledger and audit log
```

Caps (and a removal) apply on the server's next call; no restart. `status`, `admin audit`, `admin freeze` and
`admin unfreeze` take `--account ID` too; `admin freeze` without it freezes every account. `uber_status` and
`uber_account` name the caller's account in `account_id`.

## Configuration

The server (`uber-mcp`):

| Variable | Default | |
|---|---|---|
| `MCP_BEARER_TOKEN` | required for `serve` | one or more tokens (comma-separated, at least 16 characters each), so one can be rotated while the other still works |
| `UBER_STATE_DIR` | per-user folder (see [Privacy](#privacy)); `/state` in the image | the session, budget, pause, booking ledger and audit log |
| `UBER_SESSION_KEY` | unset | 32 random bytes, base64: the session is stored sealed (AES-GCM) as `session.bin`; recommended on servers |
| `UBER_MAX_FARE` | unset | the most one ride may cost, a currency code and an amount (`USD 80`, `EUR 45.50`); booking is off without it |
| `UBER_MAX_FARE_24H` | unset | the most all rides booked through this server in any 24 hours may cost, same currency; booking is off without it |
| `UBER_DAILY_BUDGET` | 1500 | Uber requests per day (resets 00:00 UTC) |
| `UBER_KEEPALIVE_SECONDS` | 14400 | how often an idle session is refreshed by reading both of Uber's sites (60 to 86400) |
| `UBER_LOGIN_URL` | unset | the login service on a private network, a name and port such as `http://uber-mcp-login:8080` (for `uber-mcp admin login`) |
| `UBER_LOGIN_TOKEN` | unset | the login service's bearer, 32 or more characters; required with `UBER_LOGIN_URL` |
| `UBER_LOGIN_VIEW_URL` | `http://localhost:8766` | where the person opens the login service's view page ([above](#on-a-server-the-login-service)) |
| `UBER_PUBLIC_URL` | unset | the HTTPS address people reach this server at (the parent of `/mcp`); with the login service it turns on sign-in links ([Signing in with a link](#signing-in-with-a-link)) |
| `UBER_TRUSTED_ACCOUNT_HEADER` | unset | the header your authenticating proxy sets to name the caller's account ([Accounts](#accounts)); only for a bearer no one but the proxy holds |
| `UBER_DEFAULT_ACCOUNT` | `default` | the id of the account kept directly in the state folder |
| `LOG_LEVEL` | INFO | |

The login service (`ghcr.io/benjamin-di/uber-mcp-login-browser`, `python -m uber_login`):

| Variable | Default | |
|---|---|---|
| `UBER_LOGIN_TOKEN` | required | the bearer `uber-mcp` sends, 32 or more characters |
| `UBER_LOGIN_SLOTS` | 1 | sign-ins at the same time |
| `UBER_LOGIN_MINUTES` | 20 | how long a sign-in may take before it is ended |
| `PORT` | 8080 | |
| `UBER_LOGIN_NOVNC` | `/opt/novnc` in the image | noVNC's files |
| `UBER_LOGIN_XVFB`, `UBER_LOGIN_X11VNC` | `Xvfb`, `x11vnc` | the virtual display and remote-desktop programs |

## Troubleshooting

| Code | What to do |
|---|---|
| `invalid_argument` | The assistant passed something malformed; the text says what. |
| `not_signed_in` | Run `uber-mcp login`, or on a server ask the assistant to connect Uber (`uber_connect`) or use `uber-mcp admin login`. |
| `session_expired` | Uber ended the sign-in: sign in again (as above). `uber_status` shows `signed_in` true once it worked. |
| `session_invalid` | The saved session cannot be read (damaged, sealed under another `UBER_SESSION_KEY`, or Uber now shows another rider): sign in again. |
| `blocked` | Uber pushed back (a 429 or a challenge page); calls resume after the pause (`uber_status.paused_until`). |
| `budget_exhausted` | Today's request budget is spent; it resets at 00:00 UTC (`UBER_DAILY_BUDGET`). |
| `busy` | Another Uber request was still running after 30 seconds; retry. |
| `upstream_error` | Uber or the network failed; retry in a minute. |
| `schema_changed` | Uber changed its web API (a renamed field, a moved endpoint). Check for a newer release, or [open an issue](https://github.com/benjamin-di/uber-mcp-server/issues); `scripts/sync-operations.py` refreshes the GraphQL documents. Other tools may still work. |
| `place_not_found` | Uber found no place: be more specific, or pass a saved place's label, a place id or `lat,lng`. |
| `no_rides` | Uber offers no rides for this trip right now; try later or from another pickup. |
| `option_not_found` | That option is not offered now, or it is reserve-only; pick one from `uber_ride_options`. |
| `payment_not_found` | No payment method matches; `uber_account` lists them. |
| `booking_disabled` | Booking is off: set both `UBER_MAX_FARE` and `UBER_MAX_FARE_24H`, or `uber-mcp admin unfreeze`. |
| `over_cap` | The price is above the per-ride cap or the 24-hour total, or in another currency than the caps (`reason`). Nothing was booked. |
| `price_rose` | The price went up since the preview; nothing was booked. The answer holds a fresh preview for the person to approve. |
| `confirm_invalid` | The token is unknown, used, expired (10 minutes), for other arguments, or was dropped by a freeze: preview again. |
| `trip_active` | A ride is already active; it must end or be cancelled first. |
| `no_active_trip` | There is no ride to cancel. |
| `uber_refused` | Uber did not accept the request (`uber_error` has Uber's key when it is a plain identifier); nothing was booked. Try in the Uber app. |
| `outcome_unknown` | A request or cancellation was sent but the answer was lost: check `uber_ride_status` (and `uber-mcp admin audit`) before anything else. |
| `account_not_connected` | The request named an account this server does not have (or a malformed one): add it and sign it in ([Accounts](#accounts)). Nothing was done. |
| `connect_unavailable` | This server makes no sign-in links (`UBER_PUBLIC_URL` or the login service is not set): sign in as for `not_signed_in`. |
| `connect_limited` | More than 6 links for this account in an hour: use the newest one, or wait. |
| `internal_error` | A bug; retry once, and [open an issue](https://github.com/benjamin-di/uber-mcp-server/issues) if it repeats. |

## How it works

Uber has no public rider API for this, so the server speaks GraphQL to the endpoints Uber's rider website uses, over
plain HTTP, with the cookies of a browser session you signed in to and the GraphQL documents Uber's own web apps
send. [docs/uber-web-api.md](https://github.com/benjamin-di/uber-mcp-server/blob/main/docs/uber-web-api.md) records what is known about those endpoints;
[docs/DESIGN.md](https://github.com/benjamin-di/uber-mcp-server/blob/main/docs/DESIGN.md) explains the decisions. When Uber changes its web API, calls fail with
`schema_changed` until the server is updated.

## Development

See [CONTRIBUTING.md](https://github.com/benjamin-di/uber-mcp-server/blob/main/CONTRIBUTING.md). Tests never contact Uber.

## Credits

Built on what [crafter-station/uber-cli](https://github.com/crafter-station/uber-cli) worked out about Uber's rider
web API: `scripts/sync-operations.py` is a port of its script that reads the GraphQL documents from Uber's own web
bundles, and the price and ride request variables follow its code (MIT; [THIRD_PARTY_NOTICES](https://github.com/benjamin-di/uber-mcp-server/blob/main/THIRD_PARTY_NOTICES)).
Design ideas also came from [AriOliv/uber-mcp](https://github.com/AriOliv/uber-mcp) (MIT).

## License

[MIT](https://github.com/benjamin-di/uber-mcp-server/blob/main/LICENSE).
