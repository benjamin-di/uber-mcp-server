"""The complete disposable Connections scenario, reported as a genuine pytest case.

Run only through check-connections.sh, outside the offline tests directory.
"""

import asyncio


def test_connections_passkeys_viewer_owner_isolation_and_disconnect():
    from connections_e2e import run

    asyncio.run(run())
