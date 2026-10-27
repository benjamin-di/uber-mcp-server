# Browser connections

Hosted browser sign-in requires an authenticated account owner. `uber_connect`
returns the fixed My connections address without creating, replacing or cancelling
an attempt. Only the authenticated owner's explicit POST Start creates one. The sign-in
window's random URL identifies an attempt; possession of it does not authorize
the page, state, start, cancel, viewer assets or WebSocket. There is no legacy
capability-link fallback when browser sign-in is configured.

The application uses standard OIDC through `signin-window[oidc]`, with Authlib
handling the authorization-code exchange, S256 PKCE and identity validation.
Any compatible OIDC provider can be configured. Application ownership, rider
identity, account storage and booking policy remain outside that integration.
The provider's tokens are discarded after callback validation; an opaque
HttpOnly browser cookie refers to a private server-side session lasting at most
15 minutes. Hosted browser authentication requires POSIX private state; local
stdio use remains available on Windows, macOS and Linux.

## Permission model

| Layer | What it authorizes |
| --- | --- |
| Human OIDC browser session | The verified owner may manage the configured existing Uber account after the required signed-group check |
| MCP gateway, such as Agentgateway | A human client or independent workload may call only the tools allowed by its issuer/audience, scopes and grant ceiling |
| Uber account and application policy | The bound rider's verified session may perform the application's allowed operations, subject to budgets, price/confirmation rules, fare caps and freeze |

Pocket ID is one standard OIDC provider that can be used here; it is not a required
vendor dependency. Human browser consent does not authorize an agent, widen an
existing agent's ceiling or create a workload identity. A workload keeps its own
reviewed MCP grant. The gateway's backend bearer/account header and the sealed Uber
session are private application credentials, never substitutes for an agent grant.

Default and named accounts use the same owner checks. The default retains its
existing state root; additional accounts have separate state and sealed sessions.
The login service owns the temporary provider browser and verifies the staged
candidate, while the application retains accepted credentials and rider binding.
No default account is exempt from hosted owner authentication. Privileged local
CLI administration is not a public browser-login route; do not expose the raw
login service or its operator view as an alternative to My connections.

The provider binding is Uber's rider UUID, checked on the native rider surface and
retained across reconnect/disconnect. Both rider/trips and ride/status surfaces must
work before staged credentials replace the accepted session. An empty account first
adopts the owner's chosen unused rider; this UUID is not proof that the rider belongs
to the Pocket ID person. The remote browser host and privileged operators are trusted
with its profile, session and entered sign-in data. Viewer lockdown does not prevent
the host operator from observing that browser.

### Group changes, logout and revocation

Signed browser identity/group claims last at most 15 minutes, or until an earlier
token expiry. Removing the required group blocks a new browser login; an already
issued browser session can remain valid until expiry. Logout ends that local
session/viewer, not the accepted Uber connection. Disconnect is the application's
immediate durable admission barrier, with already admitted operations allowed to
finish. Revoking an agent's gateway grant or token is a separate action; changing
browser groups, deleting a browser cookie or connecting Uber does not change its
audience/scopes/tool ceiling. Uber's own session revocation is separate again.
The browser's 15-minute bound is not a lifetime guarantee for either of those
other credentials. Preserve current controls and financial history through recovery.

## Configure before upgrading

When `UBER_PUBLIC_URL`, `UBER_LOGIN_URL` and `UBER_LOGIN_TOKEN` enable browser
sign-in, configure all of:

| Setting | Meaning |
| --- | --- |
| `UBER_CONNECTIONS_ISSUER` | Fixed HTTPS OIDC issuer |
| `UBER_CONNECTIONS_CLIENT_ID` | This application's registered OIDC client |
| `UBER_CONNECTIONS_GROUP` | Required group in the provider's signed claims |
| `UBER_CONNECTIONS_ACCOUNT_CLAIM` | Administrator-controlled signed claim naming an existing local account |
| `UBER_CONNECTIONS_BINDINGS_FILE` | Alternative to the account claim: private JSON mapping immutable subjects to local account ids |
| `UBER_CONNECTIONS_CLIENT_SECRET_FILE` | Optional private client-secret file when the registered client requires authentication |

Choose exactly one account mapping method. A subject map looks like
`{"example-subject":"second"}`. It must assign only one owner to each account.
The account claim must be administrator-controlled; email, display name, a
browser parameter or forwarded identity header cannot designate an owner.
No account is provisioned by signing in: create it with the existing admin
commands and configure its caps separately.

Register exactly `<UBER_PUBLIC_URL>/connections/callback` with the provider,
enable PKCE and select the provider's client authentication method deliberately.
Set the provider's application launch URL to `<UBER_PUBLIC_URL>/connections/`.
Use a dedicated browser origin for an independently trusted application; paths
and cookie names alone do not isolate scripts running under the same origin.

The reverse proxy must preserve Cookie, Set-Cookie and Origin, strip untrusted
Authorization and account headers on browser routes, and forward only:

- GET/HEAD `/connections/`, `/connections/status`, `/connections/login`,
  `/connections/callback`.
- POST `/connections/start`, `/connections/disconnect`, `/connections/logout`.
- The existing exact `/connect/<id>` page and state/viewer routes, POST start
  and cancel, and viewer WebSocket.

MCP admission remains required on `/mcp`. Connecting an Uber account grants no
agent access and changes no booking limit, confirmation, ledger or audit policy.
The application refuses to enable browser sign-in without identity configuration.
Its fixed public address is never inferred from a request Host header.

## Ownership, disconnect and recovery

My connections shows only the designated person's account and masked rider name.
An explicit first Connect action pins the verified issuer and subject to that
account. Reading status does not claim it. The owner binding and rider UUID are
retained across disconnect, so later alias reassignment cannot transfer control
or replace the rider implicitly. Reassignment requires an explicit administrator-reviewed migration; this release
provides no implicit owner transfer or self-service account replacement.

A reconnect verifies both Uber surfaces using a private staged candidate before
adopting it. Refusal, network failure, challenge, budget limits or a different
rider leave the previously accepted session untouched. The connection page
distinguishes saved credentials from live verification. A restart retains the
saved credentials; temporary sign-in windows are recreated if needed.

Disconnect first persists a new account revision and access barrier, then removes
the saved session and closes pending windows. Cleanup failure remains blocked
and offers Retry disconnect. New MCP calls, cached account admission, keepalive,
reservation monitoring and late session publication respect this barrier even
after restart or removal of browser UI configuration. Calls admitted before
disconnect may complete. Disconnect does not cancel a ride or reservation,
delete booking/audit history, or revoke Uber sessions on other devices.

Each account's private `connection.json` retains its owner, rider identity and
monotonic generation watermark. `.connection.lock` serializes session publication
and disconnect across processes. Browser sessions live separately under
`browser-sessions/`. Preserve current connection controls on backup/recovery and
runtime rollback. Older releases ignore these barriers and are incompatible
rollback candidates after activation; restoring an older control file must not
undo a disconnect.

## Acceptance

`make check` covers owner isolation, Origin, header spoofing, staged adoption,
disconnect races, retained rider identity and history, and restart with the UI
disabled. `scripts/connections_e2e.py` runs checksum-verified disposable Pocket ID,
two virtual-passkey users and the actual Xvfb/x11vnc/noVNC viewer. It checks
desktop/mobile layout, wrong-owner denial, reload, saving after page closure,
restart recovery and durable disconnect. Browser requests are intercepted and
the Uber transport is synthetic; it never uses a personal account or contacts
Uber. The CI workflow installs its pinned identity/viewer dependencies.

The candidate login-image acceptance additionally runs `scripts/paste_e2e.py`
offline to prove clipboard input, authenticated viewer access, reload and explicit
cancellation. That fixture seeds a synthetic browser session; the separate Pocket
ID acceptance performs the real OIDC and passkey exchange.

## Remaining project work

Owner-bound connections, default/named isolation, staged rider verification,
disconnect barriers and portable browser/paste acceptance are implemented. Finish:

- Genuine tagged-source and image smoke checks, then verify source/version labels
  and immutable digest provenance for both server and login image.
- Operator runtime acceptance of anonymous/wrong-owner denial, session/group expiry,
  reconnect/disconnect and existing booking limits, without requesting a real ride.
- Exact sanitized-export review and standalone public artifact qualification.

Keep destination-specific rollout steps, private grants and live evidence in the
operator's infrastructure repository. Tool annotations and client confirmation
are additional policy signals; they do not replace gateway permission checks.

## Required controls for hosted adoption

Set `CONNECTIONS_REQUIRE_CONTROLS=1` for hosted readers and credential writers.
Missing control records then refuse access, including after a state directory
is replaced. Existing standalone behavior remains available for unadopted state;
saved initialization evidence still prevents recreating a lost adopted record.

Before starting matching control-aware roles, explicitly initialize each reviewed
account in its existing private state, using the same runtime user and paths:

```sh
python -m uber_mcp.connection_state --initialize --account <configured-account>
```

This local operation makes no provider request. It preserves existing valid
controls and refuses a missing record beside retained initialization evidence.
It must not run automatically to repair a damaged or missing adopted volume.
Keep markers with current controls, bindings, credentials and accounting; check
actual mounted state before startup and recover on disposable copies. An older
backup or an image that ignores controls is not a safe way to clear a disconnect.
