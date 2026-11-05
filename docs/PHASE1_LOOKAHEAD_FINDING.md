# Phase 1 blocked: V2.1 fixture admits future bars

Date: 2026-11-05. Base revision: `9a3961787297ffb95c0690e11cbfe19916cae2bd`. Investigation only; no strategy, data, threshold or test fixture changed.

## Reproduction

`tests/test_v2_engine.py::fixture("LONG")` defines a decision boundary from its final 1m candle at `t = 42,000,000 ms` (minute 700). Its `15m`, `1h` and `4h` inputs each contain 140 bars whose last close is `125,999,999 ms`. For each higher timeframe, 94 bars close at or after t. The `1h` and `4h` fixture rows also use 15m-spaced opens despite their timeframe labels.

At the unchanged base revision, `engine.candidate("TEST", "LONG", fixture, "0.001", cfg)` returns `eligible=True`. In a copy, change **only rows with close time >= t** in those three higher timeframes to valid OHLC values (open 9, high 10, low 7, close 8). All earlier bars and 1m/5m/flow inputs remain identical. The candidate then returns `eligible=False`; `checks["15m trend"]` changes from True to False. The decision function reads the last rows of the supplied snapshot without an as-of parameter or its own closed-bar guard.

The following read-only reproduction was run on the clean snapshot of `main`:

```python
import copy
from test_v2_engine import fixture
from scalp_sentinel.engine import candidate
snapshot, cfg = fixture("LONG")
t = int(snapshot["1m"][-1][6]) + 1
later = copy.deepcopy(snapshot)
for tf in ("15m", "1h", "4h"):
    for row in later[tf]:
        if int(row[6]) >= t:
            row[1:5] = ["9", "10", "7", "8"]
before = candidate("TEST", "LONG", snapshot, "0.001", cfg)
after = candidate("TEST", "LONG", later, "0.001", cfg)
assert before.eligible and not after.eligible
```

## Scope and implication

This proves the existing V2.1 unit fixture is temporally invalid and the raw decision function is sensitive to future rows if a caller supplies them. It does **not** establish that live alerts or stored replay results used future data: `BinanceClient.snapshot` filters by close time, `replay.validate_frame` rejects future/open bars, and `swing_backtest.closed_asof` slices by decision time. Those ingestion and replay paths still need a complete parity audit before relying on historical results. A passing test built from the invalid fixture cannot establish closed-bar-only behavior.

The requested Phase 1 look-ahead tests, heartbeat, delivery retry and CI/config work are paused. Before changing the fixture or adding a decision boundary guard, obtain a decision on how to treat prior results and whether to audit archived frames and live history first. No past data or strategy rule should be silently rewritten.
