# Booking outcomes and local recovery

On-demand requests and both cancellation tools retain the outcome of each admitted confirmation.
Repeating the same token and inputs returns the recorded result without contacting Uber, including
when its preview has expired, booking is frozen or its fare has aged out of the budget window.
Account ownership and disconnect controls still apply. A new preview is a new approval, not a retry.
Reserve keeps its existing single-use confirmations and reservation resolver.

Before a money call, one atomic `booking.json` replacement consumes approval and records an opaque
operation ID, the immutable approved payload, the original operation time, exact target or bound
payment, authorization and initial unknown outcome. Request uncertainty holds the authorized fare.
Acceptance transfers that hold to booked spend once, at the original operation time. On-demand
spend and holds contribute while `at > now - 24 hours`; the operation and its replay result remain
available after that window. Reserve's unresolved holds retain their existing lifetime rule.

Only a demonstrated failure before entering the provider opener proves non-submission. After that
boundary, transport failures, remote authentication errors, generic GraphQL errors, partial data,
unsupported HTTP responses and missing/malformed results remain unknown. A clean HTTP-200 GraphQL
TripRequest result with a valid operation errorCode and no UUID or continuation proves refusal.
A UUID with a contradiction does not prove acceptance. TripCancel true acknowledges cancellation;
false or null does not prove refusal. No unknown mutation is automatically retried.

An acknowledged cancellation and a subsequently observed cancelled target are separate facts.
Another cancellation approval for that exact target is barred after unknown, acknowledgement or
observed cancellation. A proven non-submission/refusal can allow a fresh approval. Reservation
monitoring and cancellation share the action lock, including decisions, reads, failures and age-out.
Cancelled observations suppress cancellation failure only for an exact intended target; FAILED,
EXPIRED and UNFULFILLED still need attention. A terminal observation never regresses to ACTIVE or
status-unavailable. Monitoring sends no money operation.

## Inspect and resolve recorded held attempts

Use the same local configuration and account directory as the server:

```sh
uber-mcp admin attempt list --account example
uber-mcp admin attempt resolve OPERATION --account example --target-id UUID --evidence 'Independent provider correlation receipt'
uber-mcp admin attempt resolve OPERATION --account example --not-submitted --evidence 'Authoritative provider non-submission receipt'
```

List works without a session, including while disconnected, and shows only held IDs, time,
kind, hold/target and outcome facts. It does not print confirmation tokens or approved routes.
Resolution never sends a mutation. For requests, supply an independently correlated provider trip
ID: its mere existence, route, time or similarity to a preview cannot establish correlation.
The resolver reads that target and requires the exact returned ID to match before acceptance.
Cancellation resolution requires the exact approved target and a cancelled status; it records
observation without inventing acknowledgement. Cancellation never releases booked ride spend.
Missing lookup data proves nothing. `--not-submitted` is an explicit local attestation of
independently established non-submission, never a conclusion from absence in a list. Evidence is
retained privately in the ledger. No evidence means retain the held state and do not resend.
Repeated/conflicting resolutions are rejected. Existing `admin reservation` commands remain.

## Durability and installation limits

The JSON file is authoritative. Audit events remain observational: absence of a line never proves
non-submission, and audit failure cannot override an accepted result. There is no two-file commit.
Booking writes fsync the temporary file, replace atomically, and require directory fsync where
supported. Genuine directory sync errors propagate, but the new JSON may already be visible.
In that case the current call reports unknown; later guarded replay retains the strongest visible
outcome. It never restores a stale unknown snapshot or transfers accepted spend twice.
Windows and filesystems reporting unsupported directory sync provide process-crash atomicity,
not a tested host power-loss guarantee. Parent directory creation and storage hardware durability
are installation concerns. Preserve the state directory on persistent storage.

Quiesce all old money writers before upgrading; preserve booking state and audit. Old binaries do
not count the new holds and cannot be fenced by a schema marker they ignore. Do not mix versions
or roll back by restoring older state over new evidence. Unknown newer/malformed action records
stop money access; unrelated legacy keys, booked spend, Reserve records and freezes are preserved.
Old immediate previews lacking exact payment/fee binding require fresh approval.

The new map cannot reconstruct unrecorded historical requests or cancellation intentions. If old
history cannot be established independently, retain frozen booking and quiesced uncertain legacy
monitoring until installed acceptance can be established. An empty map is not proof of no earlier
effects. This patch does not import or synthesize legacy history, unblock old unknowns automatically,
or authorize an installation, live booking, export, release or deployment.
