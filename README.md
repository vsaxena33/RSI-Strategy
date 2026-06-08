# RSI Momentum Trading Engine

A real-time, event-driven algorithmic trading system for Indian equity markets (NSE) built on the Fyers WebSocket API v3.

The engine streams live tick data, builds OHLCV candles incrementally, and maintains rolling technical indicators — RSI, EMA, and SMA — in **O(1) time per tick** without rescanning historical data. All trading signals and risk management logic run on closed candles to avoid intrabar noise.

---

## Strategy

| Signal | Condition |
|--------|-----------|
| **Long entry** | RSI(28) > 65 **and** EMA(9) > SMA(9) |
| **Short entry** | RSI(28) < 35 **and** EMA(9) < SMA(9) |
| **Exit long** | RSI(28) < 65 **or** EMA(9) < SMA(9) **or** Hard TP/SL hit |
| **Exit short** | RSI(28) > 35 **or** EMA(9) > SMA(9) **or** Hard TP/SL hit |

Risk management uses a **1:2 risk-to-reward** ratio with a trailing stop that advances SL to the last closed candle's low (long) or high (short) each time price closes beyond the trigger level, shifting TP by the same amount to preserve the original reward structure.

---

## Project Structure

```
.
├── main.py              # Entry point — startup, initialization, WebSocket wiring
├── market_engine.py     # Candlestick class — WebSocket callbacks + strategy logic
├── candle_engine.py     # OHLCV candle builder + O(1) indicator update pipeline
├── RSI.py               # Rolling RSI engine (Wilder smoothing, committed/temporary state)
├── rolling_MA.py        # Rolling EMA and SMA (O(1) incremental updates)
├── historical_data.py   # Historical OHLCV data fetching from Fyers API
├── log_trade.py         # CSV trade execution logger
├── autoLogin.py         # Get access token
└── credentials.py       # client_id (not committed — see Setup)
```

---

## Architecture

```
Historical OHLCV candles (Fyers REST API)
        │
        ▼
TA-Lib initialization  ←── Called ONCE at startup for RSI, EMA, SMA
        │
        ▼
Live WebSocket ticks (Fyers WebSocket API)
        │
        ▼
candle_engine.update_live_data()
   ├── Update / create OHLCV candle
   ├── RSI.update_rsi()           O(1) Wilder step on temporary state
   ├── rolling_MA.rolling_ema()   O(1) EMA step
   └── rolling_MA.rolling_sma()   O(1) sliding-window SMA step
        │
        ▼
market_engine.Candlestick.onmessage()
   ├── Tick-level:   Hard TP/SL check on every tick
   └── Candle-level: Entry/exit signals on each newly closed candle
        │
        ▼
log_trade.log_trade()  →  trades_log.csv
```

### Committed vs Temporary RSI State

The live candle can receive thousands of ticks before it closes. To prevent Wilder-smoothed averages from drifting on every tick:

- **Committed state** (`committed_avg_gain`, `committed_avg_loss`, `committed_close`) is frozen at the last *closed* candle and never mutated during intrabar updates.
- **Temporary state** is computed each tick from the committed base using one Wilder step on a local copy.
- On candle close, temporary state is promoted to committed exactly once.

This guarantees O(1) updates with no cumulative distortion.

---

## Indicator Complexity

| Indicator | Time per tick | Memory growth |
|-----------|--------------|---------------|
| RSI (Wilder) | O(1) | O(1) |
| EMA | O(1) | O(1) |
| SMA | O(1) | O(1) |

TA-Lib is called **once per indicator at startup** for historical seeding only. All subsequent updates use pure incremental math that stays synchronized with TA-Lib output.

---

## Prerequisites

- Python 3.10+
- A Fyers API v3 account with an active app and access token

### Install dependencies

```bash
pip install fyers-apiv3 pandas numpy TA-Lib pytz
```

> TA-Lib requires the underlying C library. On Ubuntu/Debian:
> ```bash
> sudo apt-get install libta-lib-dev
> ```
> On macOS with Homebrew:
> ```bash
> brew install ta-lib
> ```

---

## Setup

1. **Clone the repository**

   ```bash
   git clone https://github.com/vsaxena33/RSI-Strategy.git
   cd RSI-Strategy
   ```

2. **Create `credentials.py`** in the project root:

   Add your FYERS API credentials inside:
        
   ```python
   credentials.py
   ```
        
   Example:
        
   ```python
   CLIENT_ID = "YOUR_CLIENT_ID"
   SECRET_KEY = "YOUR_SECRET_KEY"
   REDIRECT_URI = "YOUR_REDIRECT_URI"
   ```

3. **Generate an access token** using the Fyers API auth flow and save it:
        
   Generate the access token using:
        
   ```bash
   python autoLogin.py
   ```
        
   > Note: Due to SEBI guidelines, a new access token must be generated daily.
   
   ```bash
   echo "YOUR_ACCESS_TOKEN" > access_token.txt
   ```

5. **Configure the symbol and parameters** in `main.py`:

   ```python
   symbol     = 'NSE:RELIANCE-EQ'
   RSI_PERIOD = 28
   EMA_PERIOD = 9
   SMA_PERIOD = 9
   ```

---

## Running

```bash
python main.py
```

The engine will:
1. Fetch today's historical candles from the Fyers REST API
2. Initialize RSI, EMA, and SMA from historical data using TA-Lib
3. Connect to the Fyers WebSocket and begin processing live ticks
4. Print a live candle table to the console on every tick
5. Log all trade executions to `trades_log.csv`

---

## Trade Log

Executions are appended to `trades_log.csv` with the following schema:

```
timestamp,action,symbol,price
2026-06-05T10:32:00+05:30,Buy,NSE:RELIANCE-EQ,1452.35
2026-06-05T11:14:00+05:30,Sell,NSE:RELIANCE-EQ,1468.90
```

The file is created automatically with a header row on the first trade of each session.

---

## Key Design Decisions

**Why RSI(28) instead of the standard RSI(14)?**
RSI(28) is a slower, smoother oscillator. Combined with thresholds at 65/35 (rather than 70/30), it filters out shorter-duration noise while still capturing meaningful momentum shifts on 1-minute candles.

**Why EMA(9) vs SMA(9) for trend filter?**
EMA reacts faster to recent price action than SMA. When EMA crosses above SMA (a "golden cross" on short periods), it signals that momentum has recently accelerated — serving as a confirmation filter alongside RSI.

**Why candle-level signal gating?**
The `last_evaluated_candle` guard in `market_engine.py` ensures entry/exit logic runs exactly once per closed candle regardless of how many ticks arrive. This prevents duplicate signals and avoids reacting to intrabar price swings.

**Why committed/temporary RSI state?**
Without this separation, each tick would advance the Wilder smoothing averages, causing RSI to drift differently depending on how many ticks a candle receives — making the indicator non-reproducible. The committed/temporary split makes RSI deterministic: only the candle's final close price affects the Wilder state.

---

## Disclaimer

This project is for educational and research purposes only. It is not financial advice. Algorithmic trading involves substantial risk of loss. Always paper-trade and backtest thoroughly before deploying real capital.

---

## Author

Vaibhav Saxena
