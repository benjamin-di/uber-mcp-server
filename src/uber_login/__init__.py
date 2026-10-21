"""uber-login: the browser a person signs in to Uber with, shown to them through a remote-desktop view.

The person sees Uber's own sign-in page, running in a Chromium next to the server, through noVNC. They sign in
exactly as on uber.com (code, Google, Apple or QR code); this service automates nothing on the sign-in
(``tests/test_login_safety.py`` pins that). It only notices when Uber says signed in, opens Uber's trips site once
(Uber's single sign-on for it), checks both from the same browser, hands the session to uber-mcp once and closes
the browser. It holds no session of its own and no key; only uber-mcp can call it (a bearer).
``window.sign_in`` is the same window on the person's own screen, for ``uber-mcp login``.
"""
