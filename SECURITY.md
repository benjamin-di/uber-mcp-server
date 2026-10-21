# Security

The server holds Uber session cookies, which act like a password for the rider account they belong to, and it can
request rides that the account pays for. Please report anything that could expose the session, book or cancel a ride
without the preview and confirm step, get past the price ceiling, the fare caps or the freeze, reach a host other than
Uber's, or let the login service automate a sign-in or be used without its tokens, privately through GitHub's
[private vulnerability reporting](https://github.com/benjamin-di/uber-mcp-server/security/advisories/new) rather than
in a public issue.

Only the latest release is supported. Problems in Uber's own apps or website should be reported to Uber.
