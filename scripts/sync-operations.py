"""Refresh src/uber_mcp/operations.json from Uber's own rider web apps.

The GraphQL documents this server sends are the ones m.uber.com and riders.uber.com send themselves: this script
loads those apps' pages with a signed-in session, reads their JavaScript bundles, finds each wanted operation and
the fragments it uses, and writes them minified. It fails, naming them, when Uber renamed or removed an operation
(README "Troubleshooting": schema_changed). It is a Python port of crafter-station/uber-cli's
scripts/sync-operations.ts, used under the MIT licence (THIRD_PARTY_NOTICES).

  uv run python scripts/sync-operations.py [--cookies FILE]

The session comes from UBER_STATE_DIR (as the server's), or from --cookies: a JSON file with a "cookies" list.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src" / "uber_mcp" / "operations.json"
PAGES = ["https://m.uber.com/go/home", "https://riders.uber.com/trips"]
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
WANTED = [
    "Activities",
    "CancellationInformation",
    "CurrentUserRidersWeb",
    "GetArrears",
    "GetStatus",
    "GetTrip",
    "Products",
    "PudoLocationSearch",
    "PudoResolveLocationPudoFragment",
    "RiderCompletedTripsCount",
    "SavedPlaces",
    "TripCancel",
    "TripRequest",
]
SCRIPT = re.compile(r"https://[a-z0-9.-]+/[a-z0-9_-]+/client-[a-z0-9-]+\.js")
CHUNK_MAP = re.compile(r'(\d+):"([a-f0-9]{16})"')
OPERATION = re.compile(r"(query|mutation)\s+([A-Z][A-Za-z0-9_]+)\s*[({]")
FRAGMENT = re.compile(r"(fragment)\s+([A-Za-z0-9_]+)\s+on\s+")
SPREAD = re.compile(r"\.\.\.([A-Za-z0-9_]+)")


def cookie_header(cookies: list[dict], host: str) -> str:
    def matches(domain: str) -> bool:
        d = domain.lower().lstrip(".")
        return host == d or host.endswith("." + d)

    return "; ".join(f"{c['name']}={c['value']}" for c in cookies if matches(c.get("domain", "")))


def fetch(url: str, cookies: list[dict]) -> str:
    host = url.split("/")[2]
    headers = {"User-Agent": UA, "Accept": "text/html,*/*"}
    if host.endswith("uber.com"):  # pages only; the bundles' CDN never gets the session
        headers["Cookie"] = cookie_header(cookies, host)
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8", "replace")
    except OSError:
        return ""


def chunk_urls(page: str, cookies: list[dict]) -> list[str]:
    html = fetch(page, cookies)
    scripts = sorted(set(SCRIPT.findall(html)))
    chunks: list[str] = []
    for runtime in [s for s in scripts if "client-runtime-" in s]:
        base = runtime[: runtime.rfind("/") + 1]
        chunks += [f"{base}client-{i}-{h}.js" for i, h in CHUNK_MAP.findall(fetch(runtime, cookies))]
    return sorted(set(scripts + chunks))


def balanced_block(source: str, start: int) -> str | None:
    opening = source.find("{", start)
    if opening == -1:
        return None
    depth = 0
    for i in range(opening, len(source)):
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
        elif ch == "`":
            return None
    return None


def definitions(sources: list[str], pattern: re.Pattern[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for source in sources:
        for m in pattern.finditer(source):
            name = m.group(2)
            if name in found:
                continue
            block = balanced_block(source, m.start())
            if block:
                found[name] = block.replace("\\n", "\n")
    return found


def with_fragments(document: str, fragments: dict[str, str]) -> str:
    included: set[str] = set()
    queue = [document]
    while queue:
        for name in SPREAD.findall(queue.pop()):
            if name in included or name not in fragments:
                continue
            included.add(name)
            queue.append(fragments[name])
    return "\n".join([document, *(fragments[n] for n in sorted(included))])


def minify(document: str) -> str:
    return re.sub(r"\s+", " ", document).strip()


def load_cookies(path: str | None) -> list[dict]:
    if path:
        return json.loads(Path(path).read_text())["cookies"]
    sys.path.insert(0, str(ROOT / "src"))
    from uber_mcp.config import Settings
    from uber_mcp.session import Session, from_jar

    settings = Settings.from_env()
    _, jar, _ = Session(settings.session_path, settings.session_key).checkout()
    return from_jar(jar)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cookies", help="a JSON file with a 'cookies' list (instead of UBER_STATE_DIR)")
    args = parser.parse_args()
    cookies = load_cookies(args.cookies)
    urls = sorted({u for page in PAGES for u in chunk_urls(page, cookies)})
    if not urls:
        print("no bundles found: is the session signed in?", file=sys.stderr)
        return 1
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        bundles = [s for s in pool.map(lambda u: fetch(u, cookies), urls) if re.search(r"\b(query|mutation)\s", s)]
    operations = definitions(bundles, OPERATION)
    fragments = definitions(bundles, FRAGMENT)
    missing = [name for name in WANTED if name not in operations]
    if missing:
        print(f"missing from Uber's bundles: {', '.join(missing)}", file=sys.stderr)
        return 1
    payload = {
        "synced_at": time.strftime("%Y-%m-%d", time.gmtime()),
        "source": "Uber's rider web apps (m.uber.com, riders.uber.com), scripts/sync-operations.py",
        "operations": {name: minify(with_fragments(operations[name], fragments)) for name in WANTED},
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n")
    print(
        f"{len(WANTED)} operations from {len(urls)} bundles ({len(operations)} operations, {len(fragments)} fragments)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
