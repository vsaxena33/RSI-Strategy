"""
================================================================================
# REAL-TIME ROLLING RSI ENGINE — FYERS WEBSOCKET INTEGRATION
================================================================================

This module implements an O(1) incremental RSI (Relative Strength Index)
using Wilder's smoothing method. It mirrors the rolling EMA architecture
from the reference implementation — state is initialized once from historical
data via TA-Lib, then maintained purely through lightweight incremental math.

Architecture Overview:
──────────────────────

  Historical Data
        ↓
  RSI Initialization  (TA-Lib used ONCE to seed Wilder state)
        ↓
  Live WebSocket Ticks
        ↓
  update_live_data()  (candle builder — same pattern as EMA reference)
        ↓
  RSIState.update()   (O(1) incremental Wilder smoothing)
        ↓
  DataFrame rsi column updated

Key Design Decisions:
─────────────────────

1. COMMITTED vs TEMPORARY state:
   The current (in-progress) candle can receive thousands of ticks.
   Rather than re-running Wilder smoothing each tick over history,
   we keep a frozen "committed" state from the last CLOSED candle
   and derive the live candle's RSI on top of it without mutating
   committed state. On candle close, committed state is advanced once.

2. Wilder synchronization with TA-Lib:
   TA-Lib seeds its Wilder averages using a plain SMA over the first
   `period` changes, then switches to the recursive formula. We replicate
   this exactly during initialization so future values stay in sync.

3. Zero-loss / flat-market safety:
   avg_loss == 0  →  RSI = 100  (pure uptrend, no losses)
   avg_gain == 0  →  RSI = 0    (pure downtrend, no gains)
   Both handled explicitly without division errors.

Complexity:
───────────
  Time  : O(1) per tick / candle update
  Memory: O(1) growth regardless of dataset size (state is fixed-size)

Author: (based on architecture by Vaibhav Saxena)
================================================================================
"""

# ============================================================
# Import
# ============================================================
from fyers_apiv3.FyersWebsocket import data_ws
from fyers_apiv3 import fyersModel
from credentials import client_id
from historical_data import fetch_historical_data
from RSI import RSIState, initialize_rsi
import talib as ta
from market_engine import Candlestick

# ============================================================
# Configuration
# ============================================================
symbol     = 'NSE:RELIANCE-EQ'
timeZone   = 'Asia/Kolkata'
resolution = "1"
RSI_PERIOD = 28      # Standard Wilder period; change freely
EMA_PERIOD = 9
SMA_PERIOD = 9

# ============================================================
# Entry Point
# ============================================================
def main() -> None:
    """
    Program Startup Flow
    --------------------

    Access Token
        ↓
    Historical Data Fetch
        ↓
    TA-Lib RSI Initialization
        ↓
    Rolling RSI State Creation
        ↓
    WebSocket Connection
        ↓
    Live Tick Stream
        ↓
    Incremental RSI Updates
        ↓
    Trading Engine
    """

    # ── 1. Access token ───────────────────────────────────────
    try:
        with open('access_token.txt', 'r') as f:
            access_token = f.read().strip()
    except FileNotFoundError:
        print("Error: access_token.txt not found. Please login first.")
        exit(1)

    # ── 2. Historical candles ─────────────────────────────────
    print("Fetching historical data...")
    fyers_connection = fyersModel.FyersModel(
        client_id=client_id,
        token=access_token,
        is_async=False,
        log_path=''
    )
    historical_df = fetch_historical_data(fyers=fyers_connection)

    # ── 3. RSI initialization (TA-Lib called ONCE here) ───────
    rsi_state = RSIState(period=RSI_PERIOD)
    historical_df = initialize_rsi(historical_df, rsi_state)

    # Calculate EMA using 'close' as the source and a length of 9
    historical_df['ema_9'] = ta.EMA(historical_df['close'], 9)
    # Calculate EMA using 'close' as the source and a length of 9
    historical_df['sma_9'] = ta.MA(historical_df['close'], 9)

    # ── 4. Wire up live system ────────────────────────────────
    candlestick = Candlestick(
        data=historical_df,
        fyers=fyers_connection,
        rsi_state=rsi_state
    )

    fyersSocket = data_ws.FyersDataSocket(
        access_token=access_token,
        log_path="",
        litemode=False,
        write_to_file=False,
        reconnect=True,
        on_connect=candlestick.onopen,
        on_close=candlestick.onclose,
        on_error=candlestick.onerror,
        on_message=candlestick.onmessage
    )

    # Bind the socket instance back to engine before connecting
    candlestick.fyersSocket = fyersSocket

    print("Connecting to live stream...")
    fyersSocket.connect()

if __name__ == "__main__":
    main()
