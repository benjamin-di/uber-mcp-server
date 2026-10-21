"""uber-mcp: unofficial MCP access to an Uber rider account: places, live fares, rides behind a confirm step."""

from importlib.metadata import PackageNotFoundError, version

try:
    # The release tool bumps pyproject.toml only; read the installed distribution.
    __version__ = version("uber-mcp")
except PackageNotFoundError:  # pragma: no cover - source tree without an install
    __version__ = "0+unknown"
