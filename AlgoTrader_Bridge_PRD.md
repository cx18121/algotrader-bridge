# AlgoTrader Bridge — Product Requirements Document
# Version 1.3 | April 2026
# Updated with: Lorentzian Classification strategy context, exact alert formats, signal logic details,
# multi-timeframe support (1m/3m/5m/15m/30m/45m/1h), price deviation statistics (no blocking),
# maintenance window handling, orphan trailing stop handling, order replacement on duplicate entry signals

---

## STRATEGY CONTEXT

The TradingView strategy powering this system is "Machine Learning: Lorentzian Classification" by jdehorty (v5). Understanding how this strategy works is critical to building a correct bridge. This section documents the strategy's signal logic so the bridge can be built to correctly interpret, validate, and act on its outputs.

### What the strategy does

The strategy uses a K-Nearest Neighbors (KNN) classifier with Lorentzian distance as the distance metric instead of Euclidean distance. The rationale is that Lorentzian distance is more robust to outliers and accounts for the non-linear "warping" of price-time near major economic events (e.g., FOMC meetings, black swan events).

At each bar, the model:
1. Calculates up to 5 technical indicator features (RSI, WaveTrend, CCI, ADX — normalized to 0-1 range)
2. Iterates through historical bars (modulo 4 to ensure chronological spacing) and computes the Lorentzian distance between the current bar's features and each historical bar's features
3. Maintains a list of the k nearest neighbors (default k=8, configurable up to 100)
4. Sums the training labels of those neighbors (+1 for long, -1 for short) to produce a prediction value
5. The prediction value is an integer in the range [-k, +k] (e.g., with 8 neighbors: range is -8 to +8)
6. A positive prediction sum means the model predicts upward price movement over the next 4 bars
7. A negative prediction sum means the model predicts downward price movement

The training labels are derived from this logic: if the close price 4 bars ago is less than the current close, the label is +1 (long). If greater, it is -1 (short). The model is trained on the direction of price movement over a 4-bar window.

### Features used by the model (defaults)

Feature 1: RSI, period 14, smoothing 1
Feature 2: WaveTrend (WT), paramA=10, paramB=11
Feature 3: CCI, period 20, smoothing 1
Feature 4: ADX, period 20
Feature 5: RSI, period 9, smoothing 1

All features are normalized to the [0, 1] range before use. The specific normalization methods are:
- RSI: rescale(ema(rsi(src, n1), n2), 0, 100, 0, 1) — bounded rescale
- CCI: normalize(ema(cci(src, n1), n2), 0, 1) — unbounded normalize using historical min/max
- WT: normalize(wt1 - wt2, 0, 1) — unbounded normalize
- ADX: rescale(adx, 0, 100, 0, 1) — bounded rescale

### Filters applied before a signal is emitted

The strategy applies multiple layers of filters. A signal is only emitted as a trade entry if ALL of the following pass:

1. ML prediction filter: prediction > 0 for long, prediction < 0 for short
2. Volatility filter (default: enabled): recent ATR (1-bar) must be greater than historical ATR (10-bar). Ensures the market has enough volatility for the signal to be meaningful.
3. Regime filter (default: enabled): uses a Kalman-like filter on ohlc4. The normalized slope decline must be >= -0.1 (configurable threshold). Filters out ranging/choppy markets.
4. ADX filter (default: disabled): ADX must be above a threshold (default 20) to confirm trend strength.
5. EMA filter (default: disabled): for longs, close must be above EMA(200). For shorts, close must be below EMA(200).
6. SMA filter (default: disabled): same as EMA filter but with SMA(200).
7. Kernel regression filter (default: enabled): uses Nadaraya-Watson Rational Quadratic Kernel regression. For longs, the kernel estimate must be in a bullish state. For shorts, bearish state.

### Entry signal logic (exact Pine Script conditions)

startLongTrade = isNewBuySignal AND isBullish AND isEmaUptrend AND isSmaUptrend

Where:
  isNewBuySignal = isBuySignal AND isDifferentSignalType
  isBuySignal = (signal == direction.long) AND isEmaUptrend AND isSmaUptrend
  isDifferentSignalType = ta.change(signal) — signal direction changed from previous bar
  isBullish = kernel regression is in bullish state (if kernel filter enabled, otherwise always true)
  isEmaUptrend = close > EMA(200) if EMA filter enabled, otherwise always true
  isSmaUptrend = close > SMA(200) if SMA filter enabled, otherwise always true

startShortTrade is the mirror of startLongTrade for short direction.

This means: a long entry signal fires ONLY on the bar where the ML prediction flips from non-long to long AND all filters pass. It does not fire on every bar where the model predicts long — only on the transition bar.

### Exit signal logic

The strategy has two exit modes:

Mode 1 — Fixed exits (default):
  endLongTradeStrict: fires 4 bars after a long entry, OR when a new short signal appears within 4 bars
  endShortTradeStrict: fires 4 bars after a short entry, OR when a new long signal appears within 4 bars

Mode 2 — Dynamic exits (useDynamicExits=true):
  endLongTradeDynamic: fires when the kernel regression estimate turns bearish AND the bearish change occurred after the long entry
  endShortTradeDynamic: fires when the kernel regression estimate turns bullish AND the bullish change occurred after the short entry
  NOTE: dynamic exits are only valid when EMA filter, SMA filter, and kernel smoothing are all disabled

The strategy also has trailing stop losses configured in the Pine Script:
  Long: trail_price=high, trail_offset=trailOffset (default 50 points)
  Short: trail_price=low, trail_offset=trailOffset (default 50 points)
  The trailing stop is handled by TradingView's strategy.exit() in backtesting. It does NOT generate a separate webhook alert. The bridge must replicate this using IBKR's native TRAIL order type (see Order Router section).

### Prediction value meaning

The prediction variable is the sum of the k nearest neighbors' labels. With default k=8:
  +8 = maximum bullish conviction (all 8 neighbors predict long)
  +1 to +7 = bullish with varying conviction
  0 = neutral
  -1 to -7 = bearish with varying conviction
  -8 = maximum bearish conviction

The strategy uses this value for color-coding bars and labels. The actual entry signal is binary: does prediction cross zero and do all filters pass?

### Alert messages defined in the strategy

The strategy defines these alertcondition() calls:

1. "Open Long ▲" — fires when startLongTrade is true
   Default message: 'LDC Open Long ▲ | {{ticker}}@{{close}} | ({{interval}})'

2. "Close Long ▲" — fires when endLongTrade is true
   Default message: 'LDC Close Long ▲ | {{ticker}}@{{close}} | ({{interval}})'

3. "Open Short ▼" — fires when startShortTrade is true
   Default message: 'LDC Open Short | {{ticker}}@{{close}} | ({{interval}})'

4. "Close Short ▼" — fires when endShortTrade is true
   Default message: 'LDC Close Short ▼ | {{ticker}}@{{close}} | ({{interval}})'

5. "Open Position ▲▼" — fires when either startShortTrade or startLongTrade is true (combined, ambiguous)

6. "Close Position ▲▼" — fires when either endShortTrade or endLongTrade is true (combined, ambiguous)

7. "Kernel Bullish Color Change" — fires when kernel regression turns bullish
   Default message: 'LDC Kernel Bullish ▲ | {{ticker}}@{{close}} | ({{interval}})'

8. "Kernel Bearish Color Change" — fires when kernel regression turns bearish
   Default message: 'LDC Kernel Bearish ▼ | {{ticker}}@{{close}} | ({{interval}})'

IMPORTANT: The default alertcondition() messages are plain text, NOT JSON. The bridge must parse this plain text format OR the user must configure a custom JSON webhook body in the TradingView alert dialog. Both formats are supported; auto-detection is required.

---

## OVERVIEW

AlgoTrader Bridge is a self-hosted trading automation system. It receives buy/sell signals from the Lorentzian Classification strategy running on TradingView via HTTP webhooks, validates and risk-checks those signals, executes paper trades on Interactive Brokers (IBKR) via the TWS API using the ib_insync Python library, persists all events to a SQLite database, and surfaces everything through a real-time web dashboard with WebSocket updates.

The system is designed to run on a single Linux machine (Ubuntu 22.04 LTS). In v1.0, all trading is paper trading only. Live trading is explicitly out of scope.

---

## GOALS

- Receive TradingView webhook alerts from the Lorentzian Classification strategy in real time with signal-to-order submission latency under 500ms at p95.
- Support both plain-text LDC alert format and custom JSON format. Auto-detect which format is being used per request.
- Authenticate every incoming webhook request via a shared secret token before any processing occurs.
- Parse the signal and determine whether it is an entry (open long / open short) or exit (close long / close short).
- For entry signals: place a market order immediately followed by a trailing stop order (IBKR TRAIL type) after the entry fill.
- For exit signals: cancel the associated trailing stop order, then close the existing position with a market order.
- Apply pre-trade risk checks before every order.
- Persist every signal, order, fill, rejection, and error to a SQLite database with full audit trail.
- Stream real-time updates to a web dashboard via WebSocket.
- Support multiple tickers running the same or different LDC instances simultaneously.
- Reconnect automatically to TWS if the connection drops, within 30 seconds.

## NON-GOALS (v1.0)

- Live/real-money trading of any kind.
- Multi-broker support. IBKR only.
- Limit orders, stop orders, bracket orders on entry. Market orders only.
- Strategy authoring, backtesting, or signal generation.
- Multi-user authentication or team accounts.
- Mobile application.
- Cloud deployment automation.
- Email, SMS, or Slack alerting.
- CSV/Excel export.
- Data retention policy (SQLite grows unbounded in v1.0).
- Replicating the trailing stop logic in software. IBKR's TRAIL order type is used.
- Supporting the "Use Dynamic Exits" mode of LDC in v1.0. Kernel alerts are informational only.
- Supporting pyramiding (adding to an existing position) in v1.0.

---

## SIGNAL PARSING — DETAILED SPECIFICATION

### Two supported formats

FORMAT A — Plain text (native LDC alert format)
The strategy's built-in alertcondition() messages use this format. When TradingView fires the alert and sends the webhook, the Pine Script variables are substituted with real values.

What arrives in the POST body:
  'LDC Open Long ▲ | AAPL@182.45 | (1D)'
  'LDC Close Long ▲ | SPY@542.31 | (15)'
  'LDC Open Short | TSLA@245.10 | (1H)'
  'LDC Close Short ▼ | NVDA@890.50 | (5)'
  'LDC Kernel Bullish ▲ | SPY@542.31 | (15)'
  'LDC Kernel Bearish ▼ | SPY@541.10 | (15)'

TradingView sends plain text alerts with Content-Type: text/plain (or sometimes no Content-Type header).

FORMAT B — Custom JSON (recommended for production)
The user overrides the alert message in TradingView's alert dialog with a JSON body. This is explicit and unambiguous.

Required fields:
  action: string — one of "open_long", "close_long", "open_short", "close_short", "kernel_bullish", "kernel_bearish"
  symbol: string — ticker, will be uppercased by server

Optional fields:
  close: float — reference close price
  interval: string — chart timeframe
  strategy: string — identifier, defaults to "ldc"
  qty: integer — overrides server default for this signal only
  secret: string — alternative to X-Webhook-Secret header (for TradingView tiers that don't support custom headers)

Example JSON bodies to use in TradingView alert dialog:
  Open Long:   {"action": "open_long", "symbol": "{{ticker}}", "close": {{close}}, "interval": "{{interval}}", "strategy": "ldc"}
  Close Long:  {"action": "close_long", "symbol": "{{ticker}}", "close": {{close}}, "interval": "{{interval}}", "strategy": "ldc"}
  Open Short:  {"action": "open_short", "symbol": "{{ticker}}", "close": {{close}}, "interval": "{{interval}}", "strategy": "ldc"}
  Close Short: {"action": "close_short", "symbol": "{{ticker}}", "close": {{close}}, "interval": "{{interval}}", "strategy": "ldc"}

### Auto-detection logic

Step 1: Attempt JSON parsing of the body. If successful and "action" field is present with a valid value, treat as FORMAT B.
Step 2: If JSON parsing fails or "action" is missing, treat as FORMAT A and apply plain-text parsing.
Step 3: If neither works, return HTTP 400 with the full raw body in the error response for debugging.

### Plain text parsing rules (FORMAT A)

Given: 'LDC Open Long ▲ | AAPL@182.45 | (1D)'

Signal type detection via substring matching (case-insensitive, handle Unicode):
  Contains "Open Long"    → raw_action = "open_long"
  Contains "Close Long"   → raw_action = "close_long"
  Contains "Open Short"   → raw_action = "open_short"
  Contains "Close Short"  → raw_action = "close_short"
  Contains "Kernel Bullish" → raw_action = "kernel_bullish"
  Contains "Kernel Bearish" → raw_action = "kernel_bearish"
  Contains "Open Position" or "Close Position" → log warning, return 200 with status "unsupported", do not route

Symbol and price extraction:
  Split body on " | " to get 3 segments: prefix, ticker@price, interval
  Split middle segment on "@": symbol = left part (strip and uppercase), close_price = right part (parse as float)

Interval extraction:
  Last segment: strip parentheses, trim whitespace

Unicode handling:
  The arrow characters ▲ (U+25B2) and ▼ (U+25BC) must be handled. Do not use ASCII-only string matching.
  Ensure the server reads the request body as UTF-8.

### Normalized signal object (internal representation after parsing either format)

{
  raw_action:       str    — "open_long", "close_long", "open_short", "close_short", "kernel_bullish", "kernel_bearish"
  order_side:       str|None — "BUY" for open_long, "SELL" for open_short, "SELL" for close_long, "BUY" for close_short, None for kernel
  position_action:  str|None — "open" or "close", None for kernel
  direction:        str|None — "long" or "short", None for kernel
  symbol:           str    — uppercase ticker
  close_price:      float|None
  interval:         str|None
  strategy:         str    — "ldc" default
  qty:              int|None — resolved after parsing (see quantity resolution)
  parse_format:     str    — "json" or "plaintext"
}

### Quantity resolution

The LDC strategy does NOT include position size in its alert messages. Qty is resolved in this order:

1. If FORMAT B JSON payload includes "qty" field: use that value.
2. If env var SYMBOL_QTY_{SYMBOL} is set (e.g., SYMBOL_QTY_SPY=10): use that.
3. If env var DEFAULT_QTY is set: use that.
4. Fallback: 1 share.

For close signals (close_long, close_short): qty is always the full current open position for that symbol and direction. The resolved qty from above is ignored for close signals.

### Handling each signal type

open_long:
  Check for existing in-flight (submitted/partially_filled) long entry order: if found, apply order replacement (see SIGNAL INTEGRITY RULES — Rule 5).
  Check for existing fully filled open long position: if found, reject with reason "long position already open for {symbol}".
  Check for existing in-flight short entry order or filled short position: apply opposite-direction replacement (see Rule 5).
  Otherwise: apply risk checks, place BUY market order, then place TRAIL sell stop after fill.

open_short:
  Check for existing in-flight short entry order: if found, apply order replacement (see Rule 5).
  Check for existing filled open short position: if found, reject with reason "short position already open for {symbol}".
  Check for existing in-flight long entry order or filled long position: apply opposite-direction replacement (see Rule 5).
  Otherwise: apply risk checks, place SELL market order, then place TRAIL buy stop after fill.
  NOTE: Short selling requires the paper account to have short selling enabled.

close_long:
  Check for existing open long position. If none: log warning "close_long received but no open long position for {symbol}", return 200, do not place order.
  If position exists: cancel the trailing stop order linked to this position (look up trail_order_id), then place SELL market order for full position qty.

close_short:
  Check for existing open short position. If none: log warning, return 200, do not place order.
  If position exists: cancel the trailing stop order, then place BUY market order for full position qty.

kernel_bullish / kernel_bearish:
  Log as informational. No order placed. Return 200 with status "informational".
  Store in signals table with status "informational" for visibility on dashboard.

---

## TRAILING STOP IMPLEMENTATION

The LDC Pine Script uses strategy.exit() with trail_price=high (for longs) and trail_price=low (for shorts), with trail_offset=trailOffset (default 50 points). This trails from bar highs/lows, not from last trade price. IBKR's TRAIL order trails from last trade price. This is a slight behavioral difference but is acceptable for paper trading purposes.

### Trailing stop order construction

Immediately after an entry order fills completely (status = "filled"), place a trailing stop:

For long (entry was BUY):
  contract: same Stock contract as entry
  order.action = "SELL"
  order.orderType = "TRAIL"
  order.totalQuantity = fill_qty (from entry fill)
  order.auxPrice = TRAIL_OFFSET_POINTS (the trailing amount)
  order.trailStopPrice = fill_price - TRAIL_OFFSET_POINTS
  order.transmit = True
  NOTE: auxPrice on a TRAIL order is the trailing amount. trailStopPrice is the initial stop price.

For short (entry was SELL):
  order.action = "BUY"
  order.orderType = "TRAIL"
  order.totalQuantity = fill_qty
  order.auxPrice = TRAIL_OFFSET_POINTS
  order.trailStopPrice = fill_price + TRAIL_OFFSET_POINTS

The trail_offset from the Pine Script strategy is in "points" (price points, not ticks, not percentage). The default is 50, meaning $50 per share. This is intentionally large in the strategy to let trades run. Users should adjust via TRAIL_OFFSET_POINTS env var.

### Trailing stop order storage

After placing the trailing stop:
  Write a new orders record with order_role = "trail_stop", order_type = "TRAIL", parent_order_id = entry_order_id
  Update the entry order record: set trail_order_id = new_trail_order.id

### When a close signal arrives (model exit)

Step 1: Look up the open position for this symbol/direction. Get the associated entry order. Get trail_order_id.
Step 2: If trail_order_id is not null and trail order status is not "filled" or "cancelled":
  - Call ib.cancelOrder() for the trailing stop order
  - Wait up to 2 seconds for cancellation confirmation
  - Update trail order status to "cancelled"
Step 3: If trail order was already "filled" (trailing stop already triggered): the position is already closed. Log "close signal received but position already closed by trailing stop — no order placed". Return 200.
Step 4: Otherwise: place market close order.

### When a trailing stop triggers on IBKR

The trailing stop fill arrives via the fill event system:
  - Update the trailing stop order record to status = "filled"
  - Update the position: qty = 0, closed_at = fill_time
  - Calculate realized P&L: for long = (fill_price - avg_cost) * qty, for short = (avg_cost - fill_price) * qty
  - Push WebSocket message of type "position_update" and "order_update"
  - If a close signal arrives from TradingView after this: handle gracefully (see Step 3 above)

---

## SIGNAL INTEGRITY RULES

This section covers the 6 behaviors that determine whether a signal should be acted on, modified, or discarded. These rules are applied by the Order Router after basic parsing and authentication, before any order is placed.

---

### RULE 1: Multi-Timeframe Support

The system processes signals from multiple chart timeframes running simultaneously. Each timeframe runs an independent instance of the LDC strategy on TradingView, and each fires its own set of alerts. The bridge must correctly track which signal came from which timeframe, route orders per timeframe, and display per-timeframe statistics on the dashboard.

Supported timeframes: 1m, 3m, 5m, 15m, 30m, 45m, 1h

The interval field in the signal payload carries this information. TradingView's {{interval}} variable outputs values in the following formats:
  1 minute  → "1"
  3 minutes → "3"
  5 minutes → "5"
  15 minutes → "15"
  30 minutes → "30"
  45 minutes → "45"
  1 hour    → "60"

The server must normalize these raw TradingView interval strings to human-readable labels for display:
  "1"  → "1m"
  "3"  → "3m"
  "5"  → "5m"
  "15" → "15m"
  "30" → "30m"
  "45" → "45m"
  "60" → "1h"
  Any other value → store as-is, log a warning "unrecognized interval value: {value}"

Interval is stored on every signal record. It flows through to the order record (via signal_id join) so every order is traceable to its originating timeframe.

Position keying with multiple timeframes:
  Multiple timeframe instances of the same strategy on the same symbol must NOT share a position. A 1m LDC signal on SPY and a 15m LDC signal on SPY are independent and must be tracked as separate positions.
  The position key is: (symbol, direction, interval) — NOT just (symbol, direction).
  This means the positions table must include an interval column as part of its unique key.
  Example: SPY long from 1m chart and SPY long from 15m chart are two separate position records.

Order routing with multiple timeframes:
  When an open_long signal arrives for SPY on the 15m timeframe, the Order Router checks for an existing position with key (SPY, long, 15m). It does NOT block on a separate (SPY, long, 1m) position.
  Each timeframe's position has its own entry order, its own trailing stop, and its own close order.

Risk checks with multiple timeframes:
  MAX_POSITION_SIZE applies per (symbol, interval) position, not per symbol globally.
  MAX_OPEN_POSITIONS applies to the total count of open (symbol, interval) position records with qty > 0, across all timeframes.

Per-timeframe alert setup:
  For each timeframe you want to trade on a given symbol, you need a separate set of 4 TradingView alerts (open_long, close_long, open_short, close_short). The {{interval}} variable in the alert message body automatically includes the correct timeframe for each chart.
  Example: if you have LDC running on SPY 15m and SPY 1h, you create 8 alerts total (4 per chart).
  The JSON message body does not need to be changed between timeframes — {{interval}} handles the differentiation.

Deduplication with multiple timeframes:
  The dedup key is: (symbol, raw_action, strategy, interval). A 1m open_long and a 15m open_long for the same symbol are NOT duplicates of each other.

Schema change:
  signals table: interval column already exists. No change needed.
  positions table: add interval TEXT NOT NULL column. Update UNIQUE constraint from (symbol, direction) to (symbol, direction, interval).
  orders table: add interval TEXT column (inherited from signal at order creation time, for convenient filtering).

Dashboard changes:
  All panels that filter by symbol must also allow filtering by interval (timeframe).
  Signal Log: add Interval column.
  Order History: add Interval column.
  Open Positions: add Interval column. A user may see "SPY LONG 1m" and "SPY LONG 15m" as two separate rows.
  Add a new per-timeframe performance panel (see Dashboard section).

GET /api/signals: add interval as a query filter parameter.
GET /api/orders: add interval as a query filter parameter.
GET /api/positions: response objects include interval field.

---

### RULE 2: Price Deviation Statistics

Purpose: Track the difference between the signal's reference close price and the actual fill price. This data is stored and displayed as statistics only — it never blocks, rejects, or flags an order. It exists purely for strategy analysis.

Behavior:
  After every entry order fills completely, calculate:
    deviation_pts = fill_price - close_price (positive = filled higher than signal price, negative = filled lower)
    deviation_pct = (fill_price - close_price) / close_price * 100

  Store both values on the order record unconditionally (not only when deviation is large).
  No threshold, no warning, no flag — just data.

What this data is used for:
  The dashboard exposes per-timeframe and per-symbol slippage statistics derived from these values:
    - Average deviation (pts and %) across all fills for a given symbol/interval
    - Max deviation seen
    - Min deviation seen
    - Distribution: how many fills were within 0.1%, 0.5%, 1%, >1% of signal price

  This helps the user understand how much slippage their strategy experiences at each timeframe. High slippage on short timeframes (1m, 3m) is expected and normal. Persistent large slippage may indicate the alert delivery is delayed or the market is very fast-moving.

Schema:
  orders table: add columns signal_close_price (REAL nullable — copied from signal at order creation), fill_deviation_pts (REAL nullable), fill_deviation_pct (REAL nullable).
  These are calculated and written when the fill event is received.
  signal_close_price is copied from signal.close_price at order creation time so it is always available without joining to signals table.

New REST endpoint:
  GET /api/stats/slippage
  Auth: dashboard
  Query params: symbol (optional), interval (optional), from (optional), to (optional)
  Response 200: {
    "filters": {"symbol": str|null, "interval": str|null, "from": ISO8601|null, "to": ISO8601|null},
    "total_fills": int,
    "avg_deviation_pts": float,
    "avg_deviation_pct": float,
    "max_deviation_pts": float,
    "min_deviation_pts": float,
    "pct_within_0_1": float,
    "pct_within_0_5": float,
    "pct_within_1_0": float,
    "pct_over_1_0": float,
    "by_interval": [
      {"interval": "1m", "total_fills": int, "avg_deviation_pct": float, ...},
      ...
    ]
  }

Dashboard:
  Add a "Slippage Stats" sub-panel within the Order History panel or as a separate tab.
  Shows the statistics from GET /api/stats/slippage with interval filter dropdown.
  No color coding, no warnings — just a clean stats table.

---

### RULE 3: Maintenance Window — Close All Positions

Problem: IBKR performs daily maintenance breaks during which the TWS API is unavailable. For US equities, this is typically 11:45 PM to 12:15 AM ET (times vary by asset class). Positions held through a maintenance break risk:
  - Trailing stop orders being cancelled by IBKR during the break
  - The bridge being disconnected and unable to act on signals during the break
  - Stale signals queued during the break being executed at market open

Required behavior:
  If MAINTENANCE_WINDOW_ENABLED=true, the server must close ALL open positions and cancel ALL open orders before the maintenance window begins.

Maintenance window close sequence (triggered MAINTENANCE_CLOSE_MINUTES_BEFORE minutes before window start):

  Step 1: Stop accepting new signals. Set an internal flag: accepting_signals = false. Return HTTP 503 for any new webhook requests during this period with body {"status": "maintenance", "message": "server in pre-maintenance position close"}.

  Step 2: For each open position (qty > 0):
    a. Cancel the associated trailing stop order (if active).
    b. Place a market close order for the full position qty.
    c. Wait for fill confirmation (up to 30 seconds per position).
    d. Log the close: maintenance_position_closed with context: symbol, direction, qty, fill_price.

  Step 3: Verify all positions are closed. Query IBKR via ib.reqPositions() and confirm all position quantities are 0.
    If any position is still open after all close orders: log maintenance_close_failed with details. Alert on dashboard.

  Step 4: Cancel any remaining open orders that were not part of the close sequence (e.g., submitted entry orders that haven't filled yet).

  Step 5: Log maintenance_window_start. Set accepting_signals = false. Dashboard shows "MAINTENANCE MODE" banner.

  Step 6: At MAINTENANCE_WINDOW_END time, the server will have disconnected from TWS (IBKR drops connections). The reconnect logic handles reconnection automatically. Once TWS reconnects: set accepting_signals = true. Log maintenance_window_end.

  Step 7: After reconnect, call ib.reqPositions() to confirm position state is clean (all zeros). If any unexpected positions exist, log a warning and display on dashboard.

  Step 8: Resume normal signal processing.

Configuration:
  MAINTENANCE_WINDOW_ENABLED           Default: true
  MAINTENANCE_WINDOW_START             Default: "23:45" (11:45 PM ET). Format: "HH:MM" in ET (Eastern Time).
  MAINTENANCE_WINDOW_END               Default: "00:15" (12:15 AM ET). Format: "HH:MM" in ET.
  MAINTENANCE_CLOSE_MINUTES_BEFORE     Default: 5. Start closing positions N minutes before window.
  MAINTENANCE_TIMEZONE                 Default: "America/New_York"

Dashboard indicator:
  During maintenance mode: show a prominent red banner "MAINTENANCE MODE — No orders will be accepted until {MAINTENANCE_WINDOW_END} ET"
  During pre-maintenance close: show amber banner "PRE-MAINTENANCE CLOSE IN PROGRESS — Closing all positions"

Important note on IBKR maintenance times:
  IBKR maintenance times vary by product. US equity maintenance is ~11:45 PM–12:15 AM ET. Futures, forex, and other products have different windows. The user must configure the correct window for the instruments they are trading. The defaults cover US equities only.

---

### RULE 4: Orphan Trailing Stop Handling

Problem: A trailing stop fill event arrives from IBKR for an order that has no matching parent entry order in the bridge's database, OR a trailing stop fill arrives but the position in the database is already at qty=0 (position was already closed by another means). This can happen after:
  - A server restart where the DB was reset or corrupted
  - A manual trade placed directly in TWS
  - A race condition where a close signal and trailing stop trigger nearly simultaneously

Definition of an orphan trailing stop:
  A fill event arrives for an order with order_type="TRAIL" or action indicates a stop-type exit, AND either:
    (a) The ibkr_order_id does not match any record in the orders table, OR
    (b) The matching orders record has no parent_order_id, OR
    (c) The parent entry order's associated position already has qty=0

Behavior for case (a) — completely unknown TRAIL fill:
  - Do NOT ignore the fill. It happened on IBKR and must be recorded.
  - Create a synthetic orders record with order_role="trail_stop", status="filled", ibkr_order_id=from fill event, symbol=from fill event, action=from fill event, fill_price, fill_qty, fill_time.
  - Set parent_order_id = NULL (orphan).
  - Create a fills record linked to the synthetic order.
  - Check the positions table for this symbol. If a position with qty > 0 exists:
    - Update position to qty=0, closed_at=fill_time.
    - Log event: orphan_trail_stop_triggered with context: ibkr_order_id, symbol, fill_price, fill_qty, action.
    - Push WebSocket "position_update" and a "orphan_trail_warning" event.
    - Display on dashboard: "WARNING: Trailing stop fill received for unknown order ID {ibkr_order_id} on {symbol}. Position closed."
  - If no matching open position: log orphan_trail_no_position with context and take no position action.

Behavior for case (b) — known TRAIL order but no parent:
  - Record the fill normally.
  - Attempt to infer the position by symbol and direction.
  - If a matching open position exists: close it. Log a warning.
  - If not: log and take no position action.

Behavior for case (c) — position already closed:
  - Record the fill in the fills and orders tables (for audit purposes).
  - Do not attempt to update position (already at qty=0).
  - Log event: trail_stop_fill_position_already_closed with context.
  - Do not push a confusing position_update event. Push only an order_update event.

General rule: Never silently discard a fill event from IBKR. Every fill must be recorded, even if it cannot be fully reconciled.

---

### RULE 5: Duplicate Entry Signal — Replace Existing Order (Order Replacement)

Problem: The LDC strategy may emit a new open_long or open_short signal while a previous order for the same symbol and direction is still in-flight (submitted but not yet filled, or partially filled). In the previous PRD version, this was simply rejected. The correct behavior is to cancel the first order and replace it with the new one, because the new signal reflects updated model state.

This is distinct from the deduplication window (Rule 5 in dedup applies within 5 seconds of the same signal). Order replacement applies when:
  - A new open_long signal arrives for symbol X
  - AND an existing entry order for symbol X long is in status "submitted" or "partially_filled" (not yet fully filled)
  - AND the new signal is outside the dedup window (more than DEDUP_WINDOW_SECONDS old)

This does NOT apply if:
  - The existing order is already fully "filled" (position is open) — in that case, reject the new open signal as "position already open"
  - The existing order is for the opposite direction — that is handled as a close + open sequence (see below)

Replacement sequence:

Step 1: Detect the conflict.
  New signal: open_long for SPY.
  Existing: orders record for SPY, direction=long, order_role=entry, status="submitted" or "partially_filled".

Step 2: Cancel the existing entry order.
  Call ib.cancelOrder() for the existing entry order's ibkr_order_id.
  Wait up to 3 seconds for cancellation confirmation.
  Update the existing orders record: status="cancelled", cancelled_at=now, cancel_reason="replaced_by_signal_{new_signal_id}".

Step 3: If the existing order was partially filled before cancellation:
  The partial fill has already executed. The position now has fill_qty shares open.
  Log event: partial_fill_before_replacement with context: symbol, filled_qty, new_signal_qty.
  Options (configurable via PARTIAL_FILL_REPLACEMENT_MODE env var):
    "add" (default): place the new order for the full configured qty on top of the partial fill. The combined position will be partial_fill_qty + new_qty. This is the simplest behavior.
    "replace": close the partial fill first (place a market sell/buy for partial_fill_qty), then place the new entry for the full configured qty. This is cleaner but requires an extra order round-trip.

Step 4: Place the new entry order for the configured qty.
  Link the new order to the new signal_id.
  Set a reference on the new order: replaced_order_id = cancelled_order.id (add this column to orders table).

Step 5: Log event: order_replaced with context: symbol, old_order_id, old_ibkr_order_id, new_signal_id.

Step 6: Push WebSocket event of type "order_replaced" to dashboard.
  Dashboard shows the cancellation and new order in Order History with a visual link between them.

Schema addition:
  orders table: add columns replaced_by_signal_id (INTEGER nullable FK to signals.id) and replaced_order_id (INTEGER nullable FK to orders.id)

Opposite-direction conflict (open_long arrives while open_short is in-flight):
  This means the model has flipped direction. The correct behavior is:
  Step 1: Cancel the existing short entry order (in-flight, not yet filled).
  Step 2: If partially filled: close the partial short position first.
  Step 3: Place the new long entry order.
  This is the same replacement sequence applied across directions.

Opposite-direction conflict with filled position (open_long arrives while short position is fully open):
  This is a direction flip on a filled position.
  Step 1: The new open_long signal should be treated as: first close the short, then open the long.
  Step 2: Place a close_short market order for the full short position qty.
  Step 3: After the close fill: place the open_long entry order.
  Step 4: This happens automatically IF the LDC strategy is configured correctly — the strategy always emits a close_short before emitting an open_long (the endShortTrade condition fires before or simultaneously with startLongTrade). However, if both signals arrive in quick succession, the bridge must handle the ordering correctly.
  NOTE: In v1.0, if both a close_short and open_long arrive within the dedup window simultaneously, process close_short first, then open_long. The internal queue must preserve signal ordering. Do not process open signals in parallel with close signals for the same symbol.

---

### RULE 6: Signal Queue Ordering for Same Symbol

Problem: Multiple signals for the same symbol may arrive in rapid succession (e.g., close_short and open_long from the LDC direction flip). These must be processed in strict FIFO order per symbol. Processing them out of order could result in an open_long being placed before the close_short is executed, resulting in a simultaneous long and short position on the same symbol.

Behavior:
  The internal async queue is a single FIFO queue.
  The Order Router processes one signal at a time, in queue order.
  For signals on the same symbol: the second signal must not begin processing until the first signal's order has either been submitted to IBKR (for fire-and-forget) or rejected.
  For signals on different symbols: concurrent processing is allowed (use asyncio tasks per symbol with a per-symbol lock).

Implementation:
  Maintain a per-symbol asyncio.Lock() in the Order Router.
  When processing a signal for symbol X, acquire the lock for X.
  Release the lock after the entry or close order has been submitted to IBKR (not after fill — waiting for fill would block too long).
  This allows SPY and AAPL signals to be processed concurrently while ensuring SPY signals are processed serially.

---

## SYSTEM ARCHITECTURE

### Components

1. Webhook Server (Python/FastAPI)
   - POST /webhook with auto-format detection and signal normalization
   - Auth via X-Webhook-Secret header (or "secret" field in JSON body as fallback)
   - Deduplication, rate limiting
   - Pushes valid signals to internal async queue

2. Order Router (Python/ib_insync)
   - Consumes from async queue
   - Manages persistent TWS connection with auto-reconnect
   - Applies pre-trade risk checks
   - Places entry market orders
   - Places trailing stop orders after entry fills
   - Handles exit signals: cancel trail, place close order
   - Handles trailing stop trigger events
   - Pushes all events to WebSocket clients

3. TWS / IB Gateway (IBKR desktop app)
   - Running locally, logged into paper trading account
   - Port 7497 (paper)
   - Supports MKT and TRAIL order types natively

4. Dashboard (React SPA)
   - Served as static files
   - Panels: Status, Account, Positions (with trail stop details), Signal Log, Order History
   - Real-time via WebSocket, initial load via REST

5. Database (SQLite + SQLAlchemy)
   - signals, orders, fills, positions, account_snapshots tables

### Signal Flow

Step 1: LDC strategy fires alertcondition on bar close.
Step 2: TradingView POST to /webhook. Body is either LDC plain text or custom JSON.
Step 3: Webhook Server validates secret. Unauthorized → 401.
Step 4: Auto-detect format. Parse signal. Validate fields. Parse error → 400.
Step 5: Check dedup window. Duplicate → 200 with status "deduplicated".
Step 6: Write signal to DB. Push to async queue. Return 200 with signal_id.
Step 7: Kernel signal → log as informational, done.
Step 8 (open signal): Order Router applies risk checks. Failure → update DB, push WebSocket event, done.
Step 9 (open signal): Place market entry order. Write order to DB. Push WebSocket event.
Step 10: Entry fills. Update order record. Update position. Push WebSocket event.
Step 11: Place TRAIL stop order. Write trail order to DB. Update entry order with trail_order_id.
Step 12 (close signal): Order Router checks for open position. None → log warning, done.
Step 13 (close signal): Cancel existing TRAIL order. Place market close order. Write order to DB.
Step 14: Close order fills. Update order. Set position qty=0. Calculate P&L. Push WebSocket event.
Step 15 (trail triggers instead): Fill event received for trail order. Set position qty=0. Push WebSocket event.

---

## ORDER ROUTER — DETAILED REQUIREMENTS

### TWS Connection Management

- On startup: connect with ib_insync IB.connectAsync(). Retry every 10s if fails.
- On disconnect during operation: retry every 10s. Log each attempt.
- While disconnected: reject all signals immediately with reason "TWS disconnected".
- On reconnect: call ib.reqPositions() and ib.reqOpenOrders() to reconcile state with DB.
  - For any entry order in DB with status "submitted"/"partially_filled" that IBKR shows as filled: update DB.
  - For any entry order in DB that is filled but has no trail_order_id: re-place the trailing stop.
  - For any entry order that IBKR no longer shows as open: mark as cancelled or error.

### Pre-Route Checks (applied to all signals before risk checks)

Check A — Maintenance window:
  If accepting_signals == false (server is in maintenance mode):
  Return HTTP 503 with {"status": "maintenance"}. Do not write to signals table.
  (See SIGNAL INTEGRITY RULES — Rule 3)

### Pre-Trade Risk Checks (applied to open/entry signals only)

Check 1 — TWS connected: ib.isConnected() == True. Rejection: "TWS disconnected"

Check 2 — Existing position or in-flight order for same direction:
  For open_long: if an in-flight long entry order exists → apply order replacement (Rule 5). If a filled long position exists → reject "long position already open for {symbol}".
  For open_short: same logic for short direction.
  Rejection (filled position case): "position already open for {symbol} {direction}"

Check 3 — No pending order for symbol:
  No orders table record with symbol=X and status in ("submitted", "partially_filled")
  Rejection: "order already pending for {symbol}"

Check 4 — Max position size:
  current_position_qty + requested_qty <= MAX_POSITION_SIZE
  Rejection: "exceeds max position size of {MAX_POSITION_SIZE} for {symbol}"

Check 5 — Max open positions (only for new position, current qty=0):
  count(positions where qty>0) < MAX_OPEN_POSITIONS
  Rejection: "exceeds max open positions of {MAX_OPEN_POSITIONS}"

### Order Construction

Entry orders (all market orders):
  contract = Stock(symbol, 'SMART', 'USD')
  order = MarketOrder('BUY' or 'SELL', qty)
  trade = ib.placeOrder(contract, order)
  Write DB record immediately (before fill), store trade.order.orderId as ibkr_order_id

Trailing stop orders (placed after entry fill):
  contract = same Stock contract
  order = Order()
  order.action = 'SELL' (long) or 'BUY' (short)
  order.orderType = 'TRAIL'
  order.totalQuantity = fill_qty
  order.auxPrice = TRAIL_OFFSET_POINTS
  order.trailStopPrice = fill_price - TRAIL_OFFSET_POINTS (long) or fill_price + TRAIL_OFFSET_POINTS (short)
  order.transmit = True
  trade = ib.placeOrder(contract, order)

Close/exit orders:
  qty = current open position qty (always close full position)
  contract = Stock(symbol, 'SMART', 'USD')
  order = MarketOrder('SELL' (close long) or 'BUY' (close short), qty)
  trade = ib.placeOrder(contract, order)

### Fill Event Handling

Subscribe to fill events via ib.execDetailsEvent or Trade.fillEvent.
On each fill event:
  1. Match to orders record via ibkr_order_id. If no match found: apply orphan trailing stop logic (Rule 4).
  2. Write fills table record (every individual fill execution).
  3. Update orders record: fill_qty += this_fill.qty, recalculate fill_price as weighted average, update status.
  4. If fill_qty >= total_qty: status = "filled", fill_time = now.
  5. If this fill is for an entry order and status just became "filled":
     a. Calculate slippage: fill_deviation_pts = fill_price - signal_close_price, fill_deviation_pct = fill_deviation_pts / signal_close_price * 100. Write both to the order record. No threshold, no warning — statistics only. (See Rule 2.)
     b. Trigger trailing stop placement.
  6. If this fill is for a trailing stop order: apply orphan checks (Rule 4), then set position qty=0, closed_at=now, calculate P&L.
  7. If this fill is for a close/exit order: set position qty=0, closed_at=now, calculate P&L.
  8. Update positions table.
  9. Push WebSocket event.

---

## DATA MODEL — DETAILED SCHEMA

### Table: signals

id               INTEGER PRIMARY KEY AUTOINCREMENT
received_at      DATETIME NOT NULL     -- UTC, server receipt time
signal_time      DATETIME              -- nullable, from payload timestamp field
raw_action       TEXT NOT NULL         -- "open_long", "close_long", "open_short", "close_short", "kernel_bullish", "kernel_bearish"
order_side       TEXT                  -- "BUY", "SELL", NULL for kernel signals
position_action  TEXT                  -- "open", "close", NULL for kernel signals
direction        TEXT                  -- "long", "short", NULL for kernel signals
symbol           TEXT NOT NULL         -- uppercase
close_price      REAL                  -- nullable
interval         TEXT                  -- nullable, chart timeframe string
strategy         TEXT DEFAULT 'ldc'
qty              INTEGER               -- resolved quantity, nullable for close signals
status           TEXT NOT NULL         -- "accepted", "rejected", "deduped", "informational", "unsupported"
reject_reason    TEXT                  -- nullable
dedup_of         INTEGER               -- FK to signals.id, nullable
parse_format     TEXT NOT NULL         -- "json" or "plaintext"
raw_body         TEXT NOT NULL         -- full original request body
source_ip        TEXT

Indexes: (symbol, received_at), (raw_action), (status), (strategy)

### Table: orders

id               INTEGER PRIMARY KEY AUTOINCREMENT
signal_id        INTEGER NOT NULL      -- FK to signals.id
parent_order_id  INTEGER               -- FK to orders.id; for trail_stop orders, points to entry order
trail_order_id   INTEGER               -- FK to orders.id; for entry orders, points to associated trail_stop order
ibkr_order_id    INTEGER               -- IBKR TWS order ID
symbol           TEXT NOT NULL
action           TEXT NOT NULL         -- "BUY" or "SELL"
qty              INTEGER NOT NULL
order_type       TEXT NOT NULL         -- "MKT" or "TRAIL"
trail_amount     REAL                  -- nullable; for TRAIL orders, the trailing offset in points
trail_stop_price REAL                  -- nullable; for TRAIL orders, the initial stop price
direction        TEXT                  -- "long" or "short", which position this order is for
order_role       TEXT NOT NULL         -- "entry", "exit", "trail_stop"
status           TEXT NOT NULL         -- "pending", "submitted", "partially_filled", "filled", "cancelled", "error"
fill_qty         INTEGER DEFAULT 0
fill_price       REAL                  -- weighted average fill price
fill_time        DATETIME              -- time of complete fill
submitted_at     DATETIME
cancelled_at     DATETIME
error_msg        TEXT
replaced_by_signal_id INTEGER           -- FK to signals.id; set when this order was cancelled due to replacement
replaced_order_id     INTEGER           -- FK to orders.id; the previous order this one replaced
signal_close_price    REAL              -- copied from signal.close_price at order creation, for slippage calc
fill_deviation_pts    REAL              -- fill_price - signal_close_price, calculated on fill
fill_deviation_pct    REAL              -- fill_deviation_pts / signal_close_price * 100, calculated on fill
interval         TEXT                  -- chart timeframe inherited from signal (e.g., "1m", "15m", "1h")
created_at       DATETIME NOT NULL

Indexes: (signal_id), (symbol, status), (ibkr_order_id), (parent_order_id), (order_role)

### Table: fills

id               INTEGER PRIMARY KEY AUTOINCREMENT
order_id         INTEGER NOT NULL      -- FK to orders.id
ibkr_exec_id     TEXT                  -- IBKR execution ID (unique per fill event)
ibkr_order_id    INTEGER
fill_qty         INTEGER NOT NULL
fill_price       REAL NOT NULL
fill_time        DATETIME NOT NULL
commission       REAL                  -- nullable
exchange         TEXT                  -- nullable
created_at       DATETIME NOT NULL

Indexes: (order_id), (fill_time)

### Table: positions

id               INTEGER PRIMARY KEY AUTOINCREMENT
symbol           TEXT NOT NULL
direction        TEXT NOT NULL         -- "long" or "short"
interval         TEXT NOT NULL         -- chart timeframe this position belongs to (e.g., "1m", "15m", "1h")
qty              INTEGER NOT NULL DEFAULT 0
avg_cost         REAL
market_price     REAL
market_value     REAL
unrealized_pnl   REAL
realized_pnl     REAL                  -- cumulative realized P&L for this symbol+interval (accumulates across open/close cycles)
last_updated     DATETIME NOT NULL
opened_at        DATETIME
closed_at        DATETIME              -- set when qty reaches 0

UNIQUE constraint: (symbol, direction, interval)
Indexes: (symbol), (interval), (qty)

### Table: account_snapshots

id                    INTEGER PRIMARY KEY AUTOINCREMENT
snapshot_time         DATETIME NOT NULL
net_liquidation       REAL
total_cash            REAL
unrealized_pnl        REAL
realized_pnl          REAL
day_trades_remaining  INTEGER
equity_with_loan      REAL

---

## REST API SPECIFICATION

### POST /webhook
Auth: X-Webhook-Secret header OR "secret" field in JSON body
Content-Type: auto-detected
Response 200: {"status": "accepted"|"deduplicated"|"rejected"|"informational"|"unsupported", "signal_id": int, "reason": str|null}
Response 400: {"error": str, "raw_body": str}
Response 401: {"error": "unauthorized"}
Response 429: {"error": "rate limit exceeded"}

### GET /api/status
Auth: none
Response 200: {
  "server": "running",
  "tws_connected": bool,
  "tws_last_connected": ISO8601|null,
  "tws_disconnect_reason": str|null,
  "uptime_seconds": int,
  "signals_today": int,
  "orders_today": int,
  "open_positions": int
}

### GET /api/signals
Auth: dashboard
Query: symbol, raw_action, status, strategy, parse_format, interval, from, to, limit (default 50, max 500), offset
Response 200: {"total": int, "signals": [signal_objects]}

### GET /api/orders
Auth: dashboard
Query: symbol, order_role, order_type, status, interval, from, to, limit, offset
Response 200: {"total": int, "orders": [order_objects]}

### GET /api/positions
Auth: dashboard
Query: interval (optional filter)
Response 200: {
  "positions": [
    {
      "symbol": str, "direction": str, "interval": str, "qty": int, "avg_cost": float,
      "market_price": float, "market_value": float, "unrealized_pnl": float,
      "realized_pnl": float, "last_updated": ISO8601, "opened_at": ISO8601,
      "trail_order_id": int|null, "trail_amount": float|null, "trail_stop_price": float|null
    }
  ]
}

### GET /api/account
Auth: dashboard
Response 200: {"net_liquidation": float, "total_cash": float, "unrealized_pnl": float, "realized_pnl": float, "equity_with_loan": float, "snapshot_time": ISO8601}

### GET /api/stats/slippage
Auth: dashboard
Query: symbol (optional), interval (optional), from (optional), to (optional)
Response 200: {
  "filters": {"symbol": str|null, "interval": str|null, "from": ISO8601|null, "to": ISO8601|null},
  "total_fills": int,
  "avg_deviation_pts": float,
  "avg_deviation_pct": float,
  "max_deviation_pts": float,
  "min_deviation_pts": float,
  "pct_within_0_1": float,
  "pct_within_0_5": float,
  "pct_within_1_0": float,
  "pct_over_1_0": float,
  "by_interval": [
    {"interval": str, "total_fills": int, "avg_deviation_pts": float, "avg_deviation_pct": float, "max_deviation_pts": float, "min_deviation_pts": float}
  ]
}

### WebSocket: /ws/feed
Auth: token as query param ?token=<value>
On connect: snapshot message with current state
Message types: "signal", "order_update", "fill", "position_update", "account_update", "tws_status", "heartbeat", "order_replaced", "orphan_trail_warning", "maintenance_status"
Heartbeat sent every 30 seconds.
maintenance_status message: {"type": "maintenance_status", "data": {"mode": "normal"|"pre_close"|"maintenance", "message": str, "resumes_at": ISO8601|null}}

---

## DASHBOARD — DETAILED REQUIREMENTS

Panel 1 — Connection Status Bar (always visible)
  TWS connected/disconnected indicator, last connected time, disconnect reason if known, server uptime, signals today, orders today, open positions count.
  Active timeframes indicator: shows which intervals have received at least one signal in the current session (e.g., "1m ● 15m ● 1h ●").

Panel 2 — Account Summary
  Net Liquidation, Total Cash, Unrealized P&L (green/red), Realized P&L.

Panel 3 — Open Positions
  Columns: Symbol, Interval, Direction (LONG/SHORT), Qty, Avg Cost, Market Price, Market Value, Unrealized P&L, % Change, Trail Stop Price, Trail Offset (pts), Opened At.
  Interval column: displays normalized label (1m, 3m, 5m, 15m, 30m, 45m, 1h).
  Trail Stop Price column: current trailing stop level for the active TRAIL order. If none, show "—".
  A user may see multiple rows for the same symbol if multiple timeframes are active simultaneously (e.g., "SPY LONG 1m" and "SPY LONG 15m" as separate rows).
  Rows animate on update. Empty state: "No open positions."
  Filter: by Symbol, by Interval.

Panel 4 — Signal Log
  Columns: Time, Symbol, Interval, Action (open_long/close_long etc.), Direction, Qty, Close Price, Strategy, Format (JSON/plaintext), Status, Reason.
  Status colors: accepted (green), rejected (red), deduped (gray), informational (blue), unsupported (yellow).
  Filters: Symbol, Interval, Action, Status, Format, Date Range.
  Most recent first, last 100 shown.

Panel 5 — Order History
  Columns: Time, Symbol, Interval, Action, Qty, Type (MKT/TRAIL), Role (entry/exit/trail_stop), Status, Signal Price, Fill Price, Slippage pts, Fill Qty, Fill Time.
  Role colors: entry (green), exit (gray), trail_stop (orange).
  Signal Price column: the close_price from the originating signal (signal_close_price on the order record).
  Slippage pts column: fill_deviation_pts. Positive = filled above signal price, negative = filled below. Empty for non-entry orders (exits, trail stops).
  Cancelled orders with replaced_by_signal_id: show "→ replaced" annotation with a link to the replacement order row.
  Filters: Symbol, Interval, Role, Type, Status, Date Range.
  Most recent first, last 100 shown.

Panel 6 — Slippage Statistics
  Purpose: show how much the actual fill price deviates from the signal price, broken down by timeframe. No thresholds, no warnings — purely informational.
  Data source: GET /api/stats/slippage
  Layout:
    Top row: global summary — Total Fills, Avg Slippage (pts), Avg Slippage (%), Max Slippage (pts), Min Slippage (pts).
    Filter controls: Symbol dropdown, Interval dropdown, Date Range.
    Per-interval breakdown table:
      Columns: Interval, Fills, Avg Slip (pts), Avg Slip (%), Max Slip (pts), Min Slip (pts), Within 0.1%, Within 0.5%, Within 1.0%, Over 1.0%.
      One row per interval that has fills in the selected filter range.
    Note: slippage is expected to be higher on shorter timeframes (1m, 3m) due to faster price movement between signal fire and fill. This panel helps quantify that.
  Updates: on page load and on demand (refresh button). Not real-time streamed — pulled on demand only.

Panel 7 — Warnings
  Persistent list of warnings requiring user attention. Not auto-dismissed.
  Warning types:
    - Orphan trailing stop fills (Rule 4)
    - Maintenance close failures (Rule 3)
    - Partial fill replacements (Rule 5)
  Each warning: timestamp, symbol, interval, warning type, description, Dismiss button.
  Warning count shown as a badge on the tab.

Error handling:
  WebSocket disconnect → banner "Live feed disconnected — reconnecting..." with retry.
  Order error → persistent toast "Order error for {symbol} ({interval}): {error_msg}" until dismissed.
  TWS disconnect → status bar updates immediately, panels show stale data indicator.
  Maintenance mode → prominent banner "MAINTENANCE MODE — Resuming at {MAINTENANCE_WINDOW_END} ET".

---

## ENVIRONMENT VARIABLES — COMPLETE LIST

WEBHOOK_SECRET           Required. Shared secret for webhook auth. Min 32 chars recommended.
TWS_HOST                 Default: 127.0.0.1
TWS_PORT                 Default: 7497. WARNING: never set to 7496 in v1.0.
TWS_CLIENT_ID            Default: 1
TWS_RECONNECT_INTERVAL_SECONDS  Default: 10
DB_PATH                  Default: ./trading.db
LOG_LEVEL                Default: INFO. Values: DEBUG, INFO, WARNING, ERROR.
DEFAULT_QTY              Default: 1. Shares per order if not specified in signal.
SYMBOL_QTY_{SYMBOL}      Example: SYMBOL_QTY_SPY=10. Per-symbol qty override. Applies across all timeframes for that symbol.
SYMBOL_INTERVAL_QTY_{SYMBOL}_{INTERVAL}  Example: SYMBOL_INTERVAL_QTY_SPY_1m=5, SYMBOL_INTERVAL_QTY_SPY_15m=10. Per-symbol per-interval qty override. Takes precedence over SYMBOL_QTY_{SYMBOL}.
TRAIL_OFFSET_POINTS      Default: 50. Trailing stop offset in price points. Mirrors Pine Script trailOffset.
                         WARNING: 50 points = $50/share. Too large for stocks priced under ~$200. Adjust per instrument.
TRAIL_OFFSET_POINTS_{SYMBOL}  Example: TRAIL_OFFSET_POINTS_SPY=50, TRAIL_OFFSET_POINTS_AAPL=5. Per-symbol trail offset override.
MAX_POSITION_SIZE        Default: 1000. Max shares per (symbol, interval) position.
MAX_OPEN_POSITIONS       Default: 10. Max simultaneous open (symbol, direction, interval) position records with qty > 0.
DEDUP_WINDOW_SECONDS     Default: 5. Dedup key includes interval — 1m and 15m signals for same symbol are not duplicates.
IGNORE_SHORT_SIGNALS     Default: false. Set to true to reject all open_short signals (for accounts without short selling).
DASHBOARD_AUTH           Default: ip_allowlist. Values: ip_allowlist, basic_auth, none.
DASHBOARD_ALLOWED_IPS    Default: 127.0.0.1. Comma-separated IPs.
DASHBOARD_USERNAME       Required if DASHBOARD_AUTH=basic_auth.
DASHBOARD_PASSWORD       Required if DASHBOARD_AUTH=basic_auth.
SERVER_HOST              Default: 0.0.0.0
SERVER_PORT              Default: 8000
ACCOUNT_SNAPSHOT_INTERVAL_SECONDS  Default: 300.
MAINTENANCE_WINDOW_ENABLED       Default: true. Enables automatic position close before IBKR maintenance break.
MAINTENANCE_WINDOW_START         Default: "23:45". Time in HH:MM (24hr) Eastern Time to begin pre-maintenance close.
MAINTENANCE_WINDOW_END           Default: "00:15". Time in HH:MM (24hr) Eastern Time when maintenance ends and signals resume.
MAINTENANCE_CLOSE_MINUTES_BEFORE Default: 5. Begin closing positions this many minutes before MAINTENANCE_WINDOW_START.
MAINTENANCE_TIMEZONE             Default: "America/New_York".
PARTIAL_FILL_REPLACEMENT_MODE    Default: "add". Values: "add", "replace". Behavior when in-flight order has partial fill before replacement signal arrives.

Note on interval normalization: TradingView's {{interval}} variable outputs raw numeric strings ("1", "3", "5", "15", "30", "45", "60"). The server normalizes these to display labels ("1m", "3m", "5m", "15m", "30m", "45m", "1h") for all storage and display. Normalization happens at parse time before writing to the signals table. The raw value is preserved in raw_body only.

---

## NON-FUNCTIONAL REQUIREMENTS

Performance:
  Signal-to-order submission: < 500ms at p95
  Dashboard WebSocket update after fill: < 200ms
  Handle up to 10 concurrent webhook requests without dropping

Reliability:
  Webhook server stays up regardless of TWS state
  Auto-reconnect to TWS within 30 seconds
  On restart: reconcile positions and orders with IBKR before accepting new signals
  Every signal persisted before routing. DB write failure → 500, do not route.

Security:
  Every request authenticated. Secret never logged.
  Dashboard protected. Dashboard password never logged.
  TWS configured for localhost-only API connections.

Observability:
  All logs as structured JSON to stdout.
  Fields: time (ISO8601), level, event (snake_case), context (key-value dict).
  Key events: webhook_received, format_detected, signal_parsed, webhook_rejected_auth, webhook_rejected_validation, signal_deduped, signal_accepted, signal_informational, signal_unsupported, risk_check_failed, order_submitted, trail_order_placed, trail_order_cancelled, trail_triggered, order_filled, order_cancelled, order_error, tws_connected, tws_disconnected, tws_reconnect_attempt, position_opened, position_closed, startup_position_sync.

---

## RISKS AND MITIGATIONS

Risk: Plain text format parsing fails (Unicode, encoding issues)
  Severity: High
  Mitigation: Log full raw body on parse error. Use UTF-8 explicitly. Recommend FORMAT B (custom JSON) to all users.

Risk: Combined alert "Open Position ▲▼" used instead of directional alerts
  Severity: High
  Description: Combined alerts are ambiguous — direction cannot be determined.
  Mitigation: Return 200 with status "unsupported" and log a clear warning including the raw body. Document that combined alerts must not be used.

Risk: Close signal arrives after trailing stop has already triggered
  Severity: Medium
  Description: IBKR already closed the position via trailing stop. Model then sends close signal. Bridge would try to close a nonexistent position.
  Mitigation: Check position qty before placing close order. If qty=0: log "close signal ignored — position already closed (trail stop triggered)", return 200.

Risk: Trail offset of 50 points inappropriate for the instrument
  Severity: High
  Description: 50 points on a $50 stock = the stop is $50 below entry = 100% loss before trigger. Effectively no stop.
  Mitigation: Log a WARNING on startup and on every trail order placement if TRAIL_OFFSET_POINTS >= 20% of the instrument's close_price. Make SYMBOL-level trail offset configurable: TRAIL_OFFSET_POINTS_{SYMBOL} env var. Example: TRAIL_OFFSET_POINTS_SPY=50, TRAIL_OFFSET_POINTS_AAPL=5.

Risk: Entry fills partially, trailing stop placed for partial qty
  Severity: Low
  Description: If an entry fills in multiple partial fills, the trailing stop should be placed for the final total qty, not after each partial fill.
  Mitigation: Only trigger trailing stop placement when order status transitions to "filled" (fill_qty >= total_qty), not on each individual fill event.

Risk: Server restart while entry is filled but trailing stop not yet placed
  Severity: Medium
  Mitigation: On reconnect, query IBKR open orders and positions. Identify filled entry orders with no trail_order_id in DB. Re-place trailing stop for those.

Risk: TradingView retries webhook if it receives a non-2xx
  Severity: Medium
  Mitigation: Return 2xx for all processed signals including rejections. Dedup window catches retries within 5 seconds.

Risk: Maintenance window close fails to execute before break
  Severity: High
  Description: If close orders are placed but not filled before IBKR drops connections at the maintenance window, positions remain open through the break. IBKR may cancel trailing stops during the break.
  Mitigation: Start close sequence MAINTENANCE_CLOSE_MINUTES_BEFORE (default 5) minutes early. Use market orders (fastest fill). Wait up to 30 seconds per position for fill confirmation. If any position is still open at MAINTENANCE_WINDOW_START, log a critical error and display a prominent dashboard alert so the user can intervene manually in TWS.

Risk: Multiple timeframes open positions on the same symbol simultaneously, exceeding intended exposure
  Severity: Medium
  Description: If 1m, 15m, and 1h LDC instances all fire open_long on SPY at similar times, the user ends up with three separate SPY long positions (one per timeframe) and triple the intended exposure per symbol.
  Mitigation: Each timeframe's position is tracked independently and respects its own MAX_POSITION_SIZE. MAX_OPEN_POSITIONS counts all (symbol, direction, interval) records so the total cap is still enforced. The user should configure per-symbol-per-interval quantities deliberately via SYMBOL_INTERVAL_QTY env vars rather than relying on defaults. Document this exposure concentration risk clearly.

Risk: TradingView {{interval}} variable outputs unexpected values for non-standard timeframes
  Severity: Low
  Description: TradingView supports many timeframes. If the user adds an LDC instance on a timeframe not in the supported list (e.g., 2h = "120", 4h = "240", 1W = "1W"), the server logs a warning and stores the raw value. The position key will still include the raw interval so data integrity is maintained, but the display label will be the raw string rather than a normalized label.
  Mitigation: Log interval_not_normalized warning on parse. Store raw value. Accept the signal normally. Extend the normalization table if new timeframes are added.

Risk: Order replacement on partial fill leaves unexpected position size
  Severity: Medium
  Description: If an order for 10 shares partially fills 3 shares before being replaced by a new signal, and PARTIAL_FILL_REPLACEMENT_MODE=add, the position ends up with 3 (partial) + 10 (new) = 13 shares.
  Mitigation: Document PARTIAL_FILL_REPLACEMENT_MODE clearly. Default to "add". Log a detailed warning when this occurs. Display in Warnings panel.

Risk: Race condition between close signal and trailing stop trigger
  Severity: Medium
  Description: A close_long signal and a trailing stop fill arrive nearly simultaneously, both attempting to set position qty=0.
  Mitigation: Per-symbol asyncio lock (Rule 6) ensures serial processing. Second operation sees qty=0 and takes no position action. Both fills are still recorded.

Risk: ib_insync version incompatibility with current TWS version
  Severity: Medium
  Mitigation: Pin ib_insync version in requirements.txt. Document tested TWS version.

---

## OPEN QUESTIONS

1. Should kernel alerts be usable as an optional secondary entry confirmation? Currently informational only. Some LDC users prefer to wait for both an ML signal AND a kernel crossover before entering.

2. Should the bridge support per-symbol per-interval trail offset via env var (e.g., TRAIL_OFFSET_POINTS_SPY_1m=10, TRAIL_OFFSET_POINTS_SPY_15m=50)? Shorter timeframes may warrant tighter stops.

3. LDC sometimes produces "early signal flips" (direction changes within 4 bars, considered noise by the strategy author). Should the bridge implement a minimum bars-between-signals hold-off per (symbol, interval) on top of the existing dedup window?

4. The strategy uses trail_price=high (for longs) in strategy.exit(), meaning the stop trails from bar highs. IBKR's TRAIL order trails from last trade price. Should this behavioral difference be documented more prominently or handled differently?

5. Should the bridge support "Use Dynamic Exits" mode in v1.1? This would require treating kernel_bearish alerts as exit signals when a long position is open, and kernel_bullish as exit signals when a short position is open — per (symbol, interval).

6. If IGNORE_SHORT_SIGNALS=true, close_long signals should still be processed normally since the user may have opened a long that now needs closing. Confirm this works correctly as-is.

7. Should the dashboard display a visual link between a replaced order and its replacement in the Order History panel? E.g., a "replaced by order #{id}" annotation on the cancelled order row.

8. During the maintenance window close sequence, should the server use a hard deadline (e.g., 60 seconds before window start) and log errors for any unfilled positions rather than waiting indefinitely? A hard deadline is safer and more predictable.

9. Should orphan trailing stop fills (Rule 4) be written to a persistent warnings table so they survive server restarts and appear the next time the dashboard is opened?

10. With multiple timeframes active on the same symbol, should the Slippage Statistics panel show whether slippage is systematically higher at shorter intervals? This would validate or challenge the assumption that 1m trades experience more slippage than 15m trades.

---

## APPENDIX A — EXACT TRADINGVIEW ALERT SETUP FOR LDC (MULTI-TIMEFRAME)

You need 4 TradingView alerts per timeframe per ticker. For example, if you are running LDC on SPY at 1m, 15m, and 1h, you need 12 alerts total (4 per chart).

The JSON message body is identical across all timeframes. TradingView's {{interval}} variable automatically inserts the correct timeframe value for each chart, so there is nothing to change per timeframe — you just repeat the setup on each chart.

Supported intervals and what TradingView outputs for {{interval}}:
  1 minute chart  → {{interval}} = "1"    → server normalizes to "1m"
  3 minute chart  → {{interval}} = "3"    → server normalizes to "3m"
  5 minute chart  → {{interval}} = "5"    → server normalizes to "5m"
  15 minute chart → {{interval}} = "15"   → server normalizes to "15m"
  30 minute chart → {{interval}} = "30"   → server normalizes to "30m"
  45 minute chart → {{interval}} = "45"   → server normalizes to "45m"
  1 hour chart    → {{interval}} = "60"   → server normalizes to "1h"

Setup steps (repeat for each chart/timeframe):

1. Open the chart at the desired timeframe with LDC strategy applied.
2. Click the clock/bell "Alerts" icon in the right toolbar.
3. Click "Create Alert".
4. Condition: select "Lorentzian Classification" → "Open Long ▲"
5. Trigger: "Once Per Bar Close" (critical — prevents mid-bar repainting signals)
6. Alert actions: check "Webhook URL"
7. Webhook URL: https://your-domain.com/webhook
8. In "Message" field, paste:
   {"action": "open_long", "symbol": "{{ticker}}", "close": {{close}}, "interval": "{{interval}}", "strategy": "ldc"}
9. Click "Create".

Repeat for the other 3 signal types on the same chart:
  Close Long:  {"action": "close_long", "symbol": "{{ticker}}", "close": {{close}}, "interval": "{{interval}}", "strategy": "ldc"}
  Open Short:  {"action": "open_short", "symbol": "{{ticker}}", "close": {{close}}, "interval": "{{interval}}", "strategy": "ldc"}
  Close Short: {"action": "close_short", "symbol": "{{ticker}}", "close": {{close}}, "interval": "{{interval}}", "strategy": "ldc"}

Then open the next chart (different timeframe, same or different ticker) and repeat all 4 alerts.

The webhook URL is the same for all alerts across all timeframes. The server uses the interval field in the JSON body to route them to the correct (symbol, interval) position.

Do NOT use "Open Position ▲▼" or "Close Position ▲▼" combined alerts on any chart.

Alert count reference:
  1 symbol × 1 timeframe = 4 alerts
  1 symbol × 7 timeframes = 28 alerts
  3 symbols × 7 timeframes = 84 alerts
  TradingView alert limits vary by subscription tier. Check your plan before scaling.

Webhook secret delivery:
  Option 1 (TradingView Pro+): Add header X-Webhook-Secret: your_secret in TradingView alert advanced settings. One configuration covers all alerts.
  Option 2 (any tier): Add "secret": "your_secret_here" to each JSON body. The server validates from the body if the header is absent.

---

## APPENDIX B — TWS CONFIGURATION

1. Log into paper trading account at https://www.interactivebrokers.com/en/trading/paper-trading.php
   (Paper credentials are separate from live trading credentials)

2. Edit > Global Configuration > API > Settings:
   - Enable ActiveX and Socket Clients: CHECKED
   - Socket port: 7497
   - Allow connections from localhost only: CHECKED
   - Read-Only API: UNCHECKED (must allow order placement)
   - Master API client ID: 0

3. To enable short selling on paper account:
   Account menu > Paper Trading Account Settings > ensure short selling is not restricted.

4. Verify connectivity:
   python3 -c "from ib_insync import IB; ib = IB(); ib.connect('127.0.0.1', 7497, clientId=99); print('connected:', ib.isConnected()); ib.disconnect()"
   Expected: connected: True

---

## APPENDIX C — EXAMPLE LOG OUTPUT FOR COMPLETE LDC CYCLE

Open Long plain-text signal, parsed, risk checked, entry filled, trailing stop placed:

{"time":"2026-04-20T14:32:00.001Z","level":"INFO","event":"webhook_received","context":{"source_ip":"52.89.214.238","content_type":"text/plain"}}
{"time":"2026-04-20T14:32:00.003Z","level":"INFO","event":"format_detected","context":{"format":"plaintext"}}
{"time":"2026-04-20T14:32:00.004Z","level":"INFO","event":"signal_parsed","context":{"raw_action":"open_long","symbol":"SPY","close_price":542.31,"interval":"15","strategy":"ldc"}}
{"time":"2026-04-20T14:32:00.011Z","level":"INFO","event":"signal_accepted","context":{"signal_id":88,"symbol":"SPY","raw_action":"open_long","resolved_qty":10}}
{"time":"2026-04-20T14:32:00.098Z","level":"INFO","event":"order_submitted","context":{"order_id":55,"ibkr_order_id":10201,"symbol":"SPY","action":"BUY","qty":10,"order_type":"MKT","order_role":"entry"}}
{"time":"2026-04-20T14:32:00.743Z","level":"INFO","event":"order_filled","context":{"order_id":55,"symbol":"SPY","fill_price":542.38,"fill_qty":10}}
{"time":"2026-04-20T14:32:00.751Z","level":"INFO","event":"position_opened","context":{"symbol":"SPY","direction":"long","qty":10,"avg_cost":542.38}}
{"time":"2026-04-20T14:32:00.802Z","level":"INFO","event":"trail_order_placed","context":{"order_id":56,"parent_order_id":55,"ibkr_order_id":10202,"symbol":"SPY","action":"SELL","order_type":"TRAIL","trail_amount":50,"trail_stop_price":492.38}}

Close Long signal arrives, trail cancelled, position closed:

{"time":"2026-04-20T15:14:22.001Z","level":"INFO","event":"signal_parsed","context":{"raw_action":"close_long","symbol":"SPY","close_price":548.90}}
{"time":"2026-04-20T15:14:22.012Z","level":"INFO","event":"signal_accepted","context":{"signal_id":89,"symbol":"SPY","raw_action":"close_long","qty":10}}
{"time":"2026-04-20T15:14:22.020Z","level":"INFO","event":"trail_order_cancelled","context":{"order_id":56,"ibkr_order_id":10202,"symbol":"SPY"}}
{"time":"2026-04-20T15:14:22.095Z","level":"INFO","event":"order_submitted","context":{"order_id":57,"ibkr_order_id":10203,"symbol":"SPY","action":"SELL","qty":10,"order_type":"MKT","order_role":"exit"}}
{"time":"2026-04-20T15:14:22.701Z","level":"INFO","event":"order_filled","context":{"order_id":57,"symbol":"SPY","fill_price":548.85,"fill_qty":10}}
{"time":"2026-04-20T15:14:22.710Z","level":"INFO","event":"position_closed","context":{"symbol":"SPY","direction":"long","realized_pnl":64.70}}

Trailing stop triggers instead (position closed by stop, model close signal ignored):

{"time":"2026-04-20T14:55:10.001Z","level":"INFO","event":"trail_triggered","context":{"order_id":56,"symbol":"SPY","fill_price":492.38,"fill_qty":10}}
{"time":"2026-04-20T14:55:10.010Z","level":"INFO","event":"position_closed","context":{"symbol":"SPY","direction":"long","realized_pnl":-499.90,"closed_by":"trail_stop"}}
-- (later, model close signal arrives) --
{"time":"2026-04-20T14:58:33.001Z","level":"WARNING","event":"signal_ignored","context":{"raw_action":"close_long","symbol":"SPY","reason":"position already closed by trail_stop"}}

---

## APPENDIX D — RECOMMENDED PROJECT STRUCTURE

algotrader_bridge/
  server/
    main.py              -- FastAPI app, lifespan events (startup connect/sync, shutdown disconnect)
    config.py            -- env var loading, validation, per-symbol qty and trail offset resolution
    webhook.py           -- POST /webhook handler: auth, format detection, dedup, queue push
    signal_parser.py     -- FORMAT A plaintext parser, FORMAT B JSON parser, signal normalizer
    order_router.py      -- risk checks, entry placement, trail placement, exit placement, fill handler
    ibkr.py              -- ib_insync IB wrapper: connect, reconnect, reqPositions, reqOpenOrders
    database.py          -- engine, session factory, schema creation on startup
    models.py            -- SQLAlchemy ORM: Signal, Order, Fill, Position, AccountSnapshot
    schemas.py           -- Pydantic: WebhookRequest, SignalOut, OrderOut, PositionOut, AccountOut
    api.py               -- REST endpoints
    websocket.py         -- ConnectionManager, broadcast functions
  dashboard/
    src/
      App.jsx
      components/
        StatusBar.jsx
        AccountSummary.jsx
        Positions.jsx          -- trail stop price column
        SignalLog.jsx           -- parse_format column, action column (open_long etc.)
        OrderHistory.jsx        -- order_role column (entry/exit/trail_stop)
      hooks/
        useWebSocket.js         -- auto-reconnect, snapshot handling
      api.js
    vite.config.js
    package.json
  requirements.txt             -- pinned: fastapi, uvicorn[standard], ib_insync, sqlalchemy, aiosqlite, pydantic, python-dotenv
  .env.example
  README.md
  systemd/algotrader.service
  nginx/algotrader.conf
