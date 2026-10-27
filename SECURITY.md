# Security

The server holds Uber session cookies, which act like a password for the rider account they belong to, and it can
request rides that the account pays for. Please report anything that could expose the session, book or cancel a ride
without the preview and confirm step, get past the price ceiling, the fare caps or the freeze, reach a host other than
Uber's, or let the login service automate a sign-in or be used without its tokens, privately through GitHub's
[private vulnerability reporting](https://github.com/benjamin-di/uber-mcp-server/security/advisories/new) rather than
in a public issue.

Owner OIDC, gateway agent admission and native rider authority are separate boundaries.
Wrong-owner browser access, a forwarded attempt becoming authority, cross-account
session adoption, bypass of durable disconnect, or a browser connection widening an
agent's tool grant are in scope. Privileged operator/login endpoints must stay private.
See [the permission model](docs/CONNECTIONS.md).

Only the latest release is supported. Problems in Uber's own apps or website should be reported to Uber.
