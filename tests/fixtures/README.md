# Test fixtures

Every fixture here is synthetic. `uber.py` builds answers shaped like what Uber's rider web API sends (the same
operation names, keys, nesting, value types and formats: `fareAmountE5` integers beside display strings, `meta` as a
JSON string with an upfront signature, Google-style place ids, UUIDs, Unix-second expiries) but every value is
invented: riders, places, coordinates, prices, payment methods, drivers, plates, trips and sessions.

- Ids are zero-padded UUIDs (`00000000-0000-4000-8000-...`) and place ids are `ChIJExample...`.
- Places are `Example Home`, `Example Mall`, `Example Airport` (with terminals for `Example Air` and `Other Air`) at
  round coordinates near `10, 20`; the city is `example_city` in country `XX` with currency `XXD` (not a real
  currency), time zone `Etc/UTC`.
- The rider is `Alex Example` (`alex@example.invalid`, a decoy the code must drop), with a card `Visa ••••0000` and
  cash; the driver is `Driver Example` in a `White Make Model`, plate `XX0000`, PIN `0000`.
- `products()` covers the price kinds the code reads: two fixed upfront prices, a metered taxi range, a reserve-only
  option, and a product repeated in a second tier.
- `tests/conftest.py` holds the session (`sid`, `csid`, `jwt-session` with fake values such as `s1`) and answers the
  remaining operations inline.

Never commit a payload recorded from a real account, even trimmed. To add a fixture, copy only the shape of the
answer (keys and value types, from `src/uber_mcp/operations.json` or a response you looked at yourself), keep only the
keys the code reads, and invent every value in the same formats.
