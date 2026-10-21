# Uber's rider web API: what is known

Uber has no public API for riders to book their own rides. This server uses the GraphQL endpoints Uber's rider
website itself calls, and this page records what has been observed about them (October 2026).
`src/uber_mcp/uber.py` is the only module that sends requests; `uber.HOSTS`, `uber.READ_OPERATIONS` and
`uber.MONEY_OPERATIONS` (pinned by `tests/test_safety.py`) list every host and operation it may use. The fakes in
`tests/conftest.py` and `tests/fixtures/uber.py` reproduce the shapes below, so the tests never contact Uber.

When something here stops being true, calls fail with `schema_changed` (or the canary in `uber_status(check=true)`
names the area, such as `places_empty` or `prices_empty`): compare with this page, refresh the documents
(`scripts/sync-operations.py`), fix the module that reads the answer, add a test.

## Hosts

- `m.uber.com`: the rides web app. `POST /go/graphql` serves places, prices, the live ride, requests and
  cancellations. The sign-in starts at `/go/login-redirect?previousPathname=%2Fgo%2Fhome`.
- `riders.uber.com`: the trips site. `POST /graphql` serves the account, payment methods, trips and trip details. A
  browser that signed in on `m.uber.com` gets its cookies here through Uber's single sign-on on its first visit to
  `/trips`.
- `auth.uber.com`: Uber's sign-in pages, shown only in the sign-in window. A GraphQL call redirected there is signed
  out; the server never requests it.
- `accounts.google.com`, `appleid.apple.com`: shown in the sign-in window only for Uber's "continue with" buttons.

## Requests

- `POST` with `{"operationName", "query", "variables"}` as JSON. The documents are Uber's own
  (`src/uber_mcp/operations.json`, minified, fragments included); the server never writes its own.
- Headers: `Content-Type: application/json`, `Accept: application/json`, `Origin` and `Referer` of the endpoint's
  own host (`https://m.uber.com`, `https://m.uber.com/`), the `User-Agent` of the browser that signed in, and
  `x-csrf-token: x`. No other header was needed; the server also sends `Accept-Language: en`.
- Each host has its own cookies from the same sign-in; one cookie jar carries both.
- GraphQL answers never redirect. The server refuses bodies over 4 MiB.

## Session cookies

| Cookie | Domain | Lifetime observed | |
|---|---|---|---|
| `sid` | `.uber.com` | about 180 days | the sign-in itself |
| `csid` | `.m.uber.com`, `.riders.uber.com` (one each) | about 30 days | per site |
| `jwt-session` | per site | about 1 day | renewed by Uber on reads (`Set-Cookie` on GraphQL answers) |

Only cookies for `uber.com` and its subdomains are kept. The server writes renewed cookies back to the session file,
and its keep-alive reads both sites every 4 hours so `jwt-session` never lapses while the server runs.
`uber_status.cookie_expiry` shows the soonest expiry of each.

## Answers

| Answer | Meaning | Server |
|---|---|---|
| HTTP 401; 403 without a challenge page; a GraphQL error whose `extensions.code` is `unauthorized` or `unauthenticated` (either case); on `riders.uber.com` a 404 or a redirect to `auth.uber.com` | signed out | `session_expired`; the generation stays dead until a new sign-in |
| `CurrentUserRidersWeb` with another `uuid` than the one signed in | another rider | `session_invalid` |
| HTTP 429 | rate limited | `blocked`, pause |
| HTTP 403 or a non-JSON body with a challenge page (`cf-chl`, `challenge-platform`, `captcha`, `px-captcha`, `_pxhd`, or an HTML document) | bot defence, not the sign-in | `blocked`, pause; the session is kept |
| `GRAPHQL_VALIDATION_FAILED`, `GRAPHQL_PARSE_FAILED`, `BAD_USER_INPUT`, or a message starting `Cannot query field`, `Unknown argument`, `Unknown type`, `Variable ` | the document no longer fits Uber's schema | `schema_changed` |
| HTTP 5xx; GraphQL errors with no data | temporary | `upstream_error` |
| Any other redirect; a body that is not a JSON object | the API moved | `schema_changed` |

## Operations

| Operation | Host | Used for |
|---|---|---|
| `CurrentUserRidersWeb` | riders | Who is signed in (`uuid`, `firstName`, `rating`, `signupCountry`), payment profiles (`uuid`, `tokenType`, `displayable.displayName`), `lastSelectedPaymentProfileUuid`, rider profiles (`type: "Personal"`), Uber Cash and Uber One (`includeUserMemberships: true`). Uber's document also asks for the email address and phone number; the server drops them. Every call proves or refutes the session. |
| `GetArrears` | riders | Money owed (`hasArrears`, `amountString`, `tripUUID`). |
| `Activities` | riders | Trips: `past` (with `nextPageToken`, the server's `next_cursor`) and `upcoming`, for `orderTypes: [RIDES, TRAVEL]` and the personal profile. A card's `title` is where to, `subtitle` when, `description` the fare, `cardURL` Uber's page. |
| `GetTrip` | riders | One trip by its UUID: status, fare, driver, `waypoints` (plain strings, pickup first, drop-off last), times, surge, `receipt` (distance, duration), `mapURL`. |
| `GetStatus` | rides | Called with `latitude: 0, longitude: 0`, as the web app does before it knows the position: `coordinate` is Uber's guess of the rider's position and `city` its city (currency, time zone); `trip` is the active ride (`uuid`, `clientStatus`, `statusMessage`, `driver`, `vehicle` with `licensePlate`, `pinVerification.pin`, `etaStringShort`, `etaToDestination` in seconds, `fareString`, `cancelable`, `waypoints`). |
| `SavedPlaces` | rides | The rider's saved places with `savedPlacesMeta.label`, without coordinates. |
| `PudoLocationSearch` | rides | Place search around `latitude`/`longitude` for `type: PICKUP` or `DROPOFF`; hits without coordinates. |
| `PudoResolveLocationPudoFragment` | rides | A place by `id`, `provider` and `source` (coordinates, address, time zone and, with `includeAirportTerminals`, the terminals per airline), or an address for `latitude`/`longitude`. |
| `Products` | rides | Every ride option and its price for `pickup`, `destinations` and the payment profile ([Prices](#prices)). |
| `CancellationInformation` | rides | Uber's cancellation notice for the active ride (`title`, `chargeFee`, `messages`), with `reservationUUID: ""`. |
| `RiderCompletedTripsCount` | rides | The number of completed trips. |
| `TripRequest` | rides | **Requests a ride** (`booking.py` only). |
| `TripCancel` | rides | **Cancels a ride** (`booking.py` only): `{current: true, uuid}`, answered `true`. |

`TripRequest` takes the fresh quote's `meta` string back unchanged, the option's `vehicleView` (`id`, `description`,
`is3p`), `capacity`, `origin.location` and `destinations` as Uber's own location objects (`addressLine1`,
`addressLine2`, `coordinate`, `id`, `provider`, `source`), `locationSource: SEARCH`, `type: ON_DEMAND` and the payment
(`paymentProfileUUID`, `paymentMethodDisplayName`, `profileTokenType`, `profileType`, `profileUUID`,
`useCredits: false`). It answers `tripRequest.uuid` (the trip) or `errorCode` and `errorKey`.

## Prices

`Products` answers `products.tiers[].products[]`; a product can appear in more than one tier and is listed once. Each
product has `productUuid` (the option's id), `id` (the vehicle view id a request needs), `displayName`,
`currencyCode`, `etaInMin`, `estimatedTripTime` (seconds), `isAvailable`, `badges`, and `fares[0]`:

- `fare`: Uber's display string, in the rider's currency and format.
- `fareAmountE5`: the amount times 100,000 (divided by 1,000 it is in minor units).
- `meta`: a JSON string Uber wants back with the request.

What kind of price it is:

- **Fixed** (upfront): `meta.upfrontFare` is present, with `signature.expiresAt` (Unix seconds) about 240 seconds
  after the quote. A request needs the `meta` of a quote that is still valid, which is why the server quotes again at
  confirm. Without an expiry the server assumes 180 seconds.
- **Range** (a metered taxi): no `upfrontFare`, and the display is a range such as `40-60`. The meter decides the fare;
  `fareAmountE5` is the range's high end.
- **Reserve-only**: `isAvailable: false`, or the display `Select Time`. These can be booked ahead in the Uber app
  only.

## Places

- Uber's place search returns Google place ids (`provider: google_places`): mostly `ChIJ...`; the server also accepts
  the other prefixes Google uses (`Ei`, `Eh`, `Gh`, `Ej`, `Ek`, `EX`). The same ids work in Google Maps tools, so an
  agent can pass one from there.
- Search hits and saved places have no coordinates; resolving the id gives them, with the time zone and, for an
  airport, the terminals per airline (the server groups them by terminal).
- Resolving `latitude`/`longitude` gives the nearest address; the server keeps the point that was asked for, not
  Uber's snapped one, so a terminal's point sets the rider down there. Where Uber knows no address, the point alone
  still works.

## Keeping the documents current

`scripts/sync-operations.py` loads `https://m.uber.com/go/home` and `https://riders.uber.com/trips` with a signed-in
session (the cookies go to Uber's pages only, never to the CDN that serves the bundles), reads the `client-*.js`
bundles and the chunks their runtime lists, finds each wanted `query` or `mutation` and the fragments it spreads, and
writes them minified to `operations.json` with the date. It fails, naming them, when an operation is gone.

## Sign-in

- The sign-in window opens `/go/login-redirect` on `m.uber.com`, which sends the browser to `auth.uber.com` (a code,
  Google, Apple or a QR code, behind Uber's bot checks) and back to `m.uber.com/go/...`.
- A phone-sized window gets Uber's mobile pages only with a mobile browser identity (`User-Agent`).
- The cookies of that browser work over plain HTTP with its `User-Agent`; the session file keeps both.
