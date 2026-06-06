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
# Imports
# ============================================================
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from fyers_apiv3.FyersWebsocket import data_ws
from fyers_apiv3 import fyersModel
from credentials import client_id

import datetime as dt
import numpy as np
import pandas as pd
import pytz
import talib as ta


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
# Column Index Constants
# ============================================================
# Centralised here so any column-order change only needs one edit.
COL_OPEN   = 0
COL_HIGH   = 1
COL_LOW    = 2
COL_CLOSE  = 3
COL_VOLUME = 4
COL_RSI    = 5
COL_EMA9   = 6
COL_SMA9   = 7


# ============================================================
# RSI State Dataclass
# ============================================================
@dataclass
class RSIState:
    """
    Stores all state required for O(1) incremental RSI updates.

    Committed fields  → reflect the last fully CLOSED candle.
    Temporary fields  → reflect the currently forming (open) candle.

    Only committed fields participate in Wilder smoothing.
    Temporary fields are recomputed from scratch each tick using
    committed avg_gain / avg_loss as the base — so committed state
    is never mutated until a candle actually closes.
    """

    period: int = RSI_PERIOD

    # ── Committed state (last closed candle) ──────────────────
    committed_avg_gain: float = 0.0
    committed_avg_loss: float = 0.0
    committed_close:    float = 0.0   # close of the last closed candle

    # ── Temporary state (current live candle) ─────────────────
    # These are recomputed on every tick and never stored back into
    # committed fields until the candle closes.
    current_rsi:      float = np.nan
    current_avg_gain: float = 0.0
    current_avg_loss: float = 0.0

    # Indicates whether we have enough history to produce valid RSI
    is_ready: bool = False


# ============================================================
# RSI Helper: Wilder Smoothing
# ============================================================
def _wilder_smooth(prev_avg: float, new_value: float, period: int) -> float:
    """
    One step of Wilder's smoothing (also called Wilder's EMA):

        new_avg = ((prev_avg × (period − 1)) + new_value) / period

    This is equivalent to an EMA with multiplier 1/period instead of 2/(period+1).

    Parameters
    ----------
    prev_avg  : Previous smoothed average (gain or loss).
    new_value : Current period's raw gain or loss.
    period    : RSI period (default 28).

    Returns
    -------
    float : Updated smoothed average.
    """
    return (prev_avg * (period - 1) + new_value) / period


# ============================================================
# RSI Helper: RSI from avg_gain / avg_loss
# ============================================================
def _compute_rsi(avg_gain: float, avg_loss: float) -> float:
    """
    Convert Wilder-smoothed gain and loss averages into an RSI value.

    Edge cases:
      avg_loss == 0 and avg_gain >  0  →  pure uptrend  →  RSI = 100
      avg_loss == 0 and avg_gain == 0  →  flat market   →  RSI = 50  (neutral)
      avg_gain == 0 and avg_loss >  0  →  pure downtrend→  RSI = 0

    Parameters
    ----------
    avg_gain : Wilder-smoothed average gain.
    avg_loss : Wilder-smoothed average loss.

    Returns
    -------
    float : RSI in range [0, 100].
    """
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ============================================================
# RSI Initialization from Historical Data
# ============================================================
def initialize_rsi(df: pd.DataFrame, state: RSIState) -> pd.DataFrame:
    """
    Seed the RSIState from historical OHLCV data.

    Strategy
    ─────────
    1. Use TA-Lib to compute RSI over the full history (ONCE, at startup).
       This populates the 'rsi' column in the DataFrame.

    2. Back-calculate the committed Wilder state (avg_gain, avg_loss)
       from the LAST valid RSI value so future incremental updates
       stay in sync with TA-Lib's output.

    3. Mark state.is_ready = True only when we have enough candles.

    Why back-calculate instead of re-running Wilder from scratch?
    ─────────────────────────────────────────────────────────────
    We have the final RSI value from TA-Lib. From RSI we can recover RS,
    and from RS and avg_loss we can recover avg_gain. This is more robust
    than reimplementing TA-Lib's exact seeding order (SMA seed → Wilder).

    Parameters
    ----------
    df    : Historical OHLCV DataFrame (must have 'close' column).
    state : RSIState instance to populate.

    Returns
    -------
    pd.DataFrame : Same df with 'rsi' column added.
    """

    close = df['close'].values

    if len(close) < state.period + 1:
        # Not enough candles yet; RSI will be NaN until more data arrives
        df['rsi'] = np.nan
        state.is_ready = False
        print(f"[RSI Init] Only {len(close)} candles — need {state.period + 1}. RSI pending.")
        return df

    # ── Step 1: TA-Lib computes RSI for the full history ──────
    rsi_series = ta.RSI(close, timeperiod=state.period)
    df['rsi'] = rsi_series

    # ── Step 2: Find the last valid (non-NaN) RSI value ───────
    last_valid_idx = None
    for i in range(len(rsi_series) - 1, -1, -1):
        if not np.isnan(rsi_series[i]):
            last_valid_idx = i
            break

    if last_valid_idx is None:
        state.is_ready = False
        print("[RSI Init] TA-Lib returned all NaN — insufficient history.")
        return df

    last_rsi = rsi_series[last_valid_idx]

    # ── Step 3: Back-calculate Wilder avg_gain and avg_loss ───
    #
    # From:  RSI = 100 - 100 / (1 + RS)
    # →      RS  = (100 - RSI) can't be used directly, so:
    # →      RS  = RSI / (100 - RSI)          [standard inversion]
    #
    # From:  RS  = avg_gain / avg_loss
    # We need a second equation. We use the Wilder step applied
    # to the change at last_valid_idx to recover avg_loss, then derive avg_gain.
    #
    # Alternative (simpler and equally accurate):
    # Re-run Wilder forward from the SMA seed up to last_valid_idx.
    # This mirrors TA-Lib's internal logic exactly.

    # Re-run Wilder from the beginning to recover exact state
    changes   = np.diff(close[:last_valid_idx + 1])  # length = last_valid_idx
    gains     = np.where(changes > 0, changes,  0.0)
    losses    = np.where(changes < 0, -changes, 0.0)

    # SMA seed over first `period` changes (indices 0 .. period-1)
    avg_gain = float(np.mean(gains[:state.period]))
    avg_loss = float(np.mean(losses[:state.period]))

    # Wilder smoothing for remaining changes (indices period .. last_valid_idx-1)
    for i in range(state.period, len(changes)):
        avg_gain = _wilder_smooth(avg_gain, gains[i],  state.period)
        avg_loss = _wilder_smooth(avg_loss, losses[i], state.period)

    # Committed state now reflects the candle at last_valid_idx
    state.committed_avg_gain = avg_gain
    state.committed_avg_loss = avg_loss
    state.committed_close    = float(close[last_valid_idx])
    state.is_ready           = True

    # Also set current_rsi to the last known value (for the live candle display)
    state.current_rsi      = last_rsi
    state.current_avg_gain = avg_gain
    state.current_avg_loss = avg_loss

    print(f"[RSI Init] Ready. Last RSI={last_rsi:.2f} | "
          f"avg_gain={avg_gain:.4f} | avg_loss={avg_loss:.4f} | "
          f"committed_close={state.committed_close:.2f}")

    return df


# ============================================================
# Rolling RSI Update (O(1) per tick)
# ============================================================
def update_rsi(
    state:          RSIState,
    current_close:  float,
    candle_closed:  bool = False
) -> float:
    """
    Update RSI state given a new close price.

    This function is called:
      • On EVERY tick (candle_closed=False) → updates temporary state only.
      • When a candle CLOSES      (candle_closed=True)  → advances committed state.

    Design
    ──────
    Committed state (avg_gain / avg_loss / close from last closed candle) is
    NEVER touched unless candle_closed=True. This means tick-by-tick updates
    are pure reads + one Wilder step on a local copy, keeping committed state
    frozen until the candle actually finalises.

    Parameters
    ----------
    state         : Live RSIState instance.
    current_close : Latest close price (updated each tick).
    candle_closed : True only when the candle has fully closed.

    Returns
    -------
    float : Current RSI value (temporary for live candle, committed on close).
    """

    if not state.is_ready:
        return np.nan

    # ── Compute gain/loss relative to last committed close ────
    change = current_close - state.committed_close
    gain   = max(change,  0.0)
    loss   = max(-change, 0.0)

    # ── One Wilder step on a LOCAL copy (never mutates committed state) ──
    temp_avg_gain = _wilder_smooth(state.committed_avg_gain, gain,  state.period)
    temp_avg_loss = _wilder_smooth(state.committed_avg_loss, loss,  state.period)

    # ── Compute temporary RSI ─────────────────────────────────
    temp_rsi = _compute_rsi(temp_avg_gain, temp_avg_loss)

    # Update temporary fields on state for external access
    state.current_avg_gain = temp_avg_gain
    state.current_avg_loss = temp_avg_loss
    state.current_rsi      = temp_rsi

    if candle_closed:
        # ── Promote temporary → committed ─────────────────────
        # This happens exactly ONCE per closed candle, not per tick.
        state.committed_avg_gain = temp_avg_gain
        state.committed_avg_loss = temp_avg_loss
        state.committed_close    = current_close

    return temp_rsi


# ============================================================
# Rolling EMA Calculation for Live Candle
# ============================================================
def rolling_ema(ltp, prev_ema, length):
    """
    Calculate the Exponential Moving Average (EMA) for a new tick using the previous EMA value.

    Parameters
    ----------
    ltp : float
        The latest traded price (current market price).

    prev_ema : float
        The EMA value from the last closed candle.

    length : int
        The period length for the EMA calculation (e.g., 9 or 15).

    Returns
    -------
    float
        The updated EMA value based on the latest tick.
    """
    multiplier = 2 / (length + 1)
    new_ema = (ltp - prev_ema) * multiplier + prev_ema
    return new_ema


# ============================================================
# Rolling SMA Calculation for Live Candle
# ============================================================
def rolling_sma(df: pd.DataFrame, ltp: float, length: int) -> float:
    """
    Sliding-window SMA step using the closed DataFrame (before the new candle
    has been appended):
 
        new_sma = prev_sma + (ltp − oldest_close) / length
 
    'oldest_close' is the candle that is dropping out of the window.
    When called for a new candle:
        prev_sma    = df['sma_9'].iloc[-1]   (last closed candle's SMA)
        oldest_close = df['close'].iloc[-length]  (the candle leaving the window)
 
    Parameters
    ----------
    df     : DataFrame BEFORE the new candle is appended.
    ltp    : The new candle's first tick price.
    length : SMA period (e.g. 9).
    """
    prev_sma     = df['sma_9'].iloc[-1]
    oldest_close = df['close'].iloc[-length]   # candle rolling out of window
    return prev_sma + (ltp - oldest_close) / length


# ============================================================
# OHLCV Candle Builder with Rolling RSI Integration
# ============================================================
def update_live_data(
    data:              pd.DataFrame,
    message:           dict,
    last_total_volume: Optional[int],
    rsi_state:         RSIState
) -> tuple[pd.DataFrame, Optional[int]]:
    """
    Update the OHLCV DataFrame using an incoming WebSocket tick message,
    and maintain the rolling RSI column.

    Mirrors the EMA reference implementation exactly.
    RSI is updated on every tick but committed state only advances on candle close.

    Parameters
    ----------
    data              : Existing OHLCV DataFrame indexed by minute timestamp.
    message           : Incoming Fyers WebSocket tick dict.
    last_total_volume : Previous cumulative traded volume.
    rsi_state         : Live RSIState instance.

    Returns
    -------
    tuple[pd.DataFrame, Optional[int]]
        Updated DataFrame and latest cumulative volume.
    """

    # ── Guard: malformed message ───────────────────────────────
    if "symbol" not in message:
        return data, last_total_volume

    ltp = message.get('ltp')
    if ltp is None:
        return data, last_total_volume

    # ── Volume: cumulative → incremental ──────────────────────
    total_vol = message.get('vol_traded_today')
    timestamp = pd.Timestamp.now(tz=timeZone).floor('1min')

    if total_vol is None:
        total_vol = last_total_volume if last_total_volume is not None else 0

    if last_total_volume is None:
        incremental_vol = 0
    else:
        incremental_vol = max(total_vol - last_total_volume, 0)

    # ── UPDATE existing candle ────────────────────────────────
    if len(data) > 0 and data.index[-1] == timestamp:
        data.iloc[-1, COL_CLOSE]   = ltp
        data.iloc[-1, COL_HIGH]    = max(data.iloc[-1, COL_HIGH], ltp)
        data.iloc[-1, COL_LOW]     = min(data.iloc[-1, COL_LOW],  ltp)
        data.iloc[-1, COL_VOLUME] += incremental_vol

        # Update RSI temporarily (candle not yet closed)
        live_rsi = update_rsi(rsi_state, current_close=ltp, candle_closed=False)
        data.iloc[-1, COL_RSI] = live_rsi
        # Recalculating EMAs of Live Candle
        if len(data) >= 2:
            prev_ema_9 = data.iloc[-2, COL_EMA9]   # last closed candle's ema_9
            prev_sma_9 = data.iloc[-2, COL_SMA9]   # last closed candle's sma_9
            
            # EMA values are updated continuously during candle formation.
            #
            # This provides a live estimate of EMA movement before
            # candle close and allows the chart to display a more
            # realistic real-time trend representation.
            #
            # Trading decisions are still made only on closed candles.
            data.iloc[-1, COL_EMA9] = rolling_ema(ltp, prev_ema_9, EMA_PERIOD) if pd.notna(prev_ema_9) else np.nan
            # For SMA mid-candle: slide against the base snapshot taken at candle open
            # We reuse the same oldest_close that was used when this candle was created.
            # The simplest correct approach: recompute from prev_sma + (ltp - oldest_close)/length
            # oldest_close is at position -(SMA_PERIOD) relative to the last CLOSED candle (iloc[-2])
            if pd.notna(prev_sma_9) and len(data) > SMA_PERIOD:
                oldest_close = data['close'].iloc[-(SMA_PERIOD + 1)]  # drops out of window for this candle
                data.iloc[-1, COL_SMA9] = prev_sma_9 + (ltp - oldest_close) / SMA_PERIOD
            else:
                data.iloc[-1, COL_SMA9] = np.nan

    else:
        # ── CLOSE previous candle and CREATE new one ──────────
        #
        # If there was a previous live candle, commit its RSI state.
        # We use the previous candle's close as the "candle closed" price.
        if len(data) > 0:
            prev_close = float(data.iloc[-1, COL_CLOSE])
            # Advance committed state using the now-finalised candle close
            update_rsi(rsi_state, current_close=prev_close, candle_closed=True)

        # Compute the very first tick RSI for the brand new candle
        # (candle not closed yet — just started)
        new_rsi  = update_rsi(rsi_state, current_close=ltp, candle_closed=False)
 
        prev_ema_9 = data.iloc[-1, COL_EMA9] if len(data) > 0 else np.nan
        prev_sma_9 = data.iloc[-1, COL_SMA9] if len(data) > 0 else np.nan
 
        new_ema_9 = rolling_ema(ltp, prev_ema_9, EMA_PERIOD) if pd.notna(prev_ema_9) else np.nan
 
        # rolling_sma needs the pre-append DataFrame and at least SMA_PERIOD+1 candles
        if pd.notna(prev_sma_9) and len(data) >= SMA_PERIOD:
            new_sma_9 = rolling_sma(data, ltp, SMA_PERIOD)
        else:
            new_sma_9 = np.nan

        new_candle = pd.DataFrame(
            [{
                'open':   ltp,
                'high':   ltp,
                'low':    ltp,
                'close':  ltp,
                'volume': incremental_vol,
                'rsi':    new_rsi,
                'ema_9':  new_ema_9,
                'sma_9':  new_sma_9
            }],
            index=[timestamp]
        )
        data = pd.concat([data, new_candle])

    return data, total_vol


# ============================================================
# Historical Data Fetching
# ============================================================
def fetch_historical_data(fyers: fyersModel.FyersModel) -> pd.DataFrame:
    """
    Fetch intraday OHLCV candles from market open to now.
    """
    now          = dt.datetime.now(pytz.timezone(timeZone))
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)

    nifty_data = {
        "symbol":      symbol,
        "resolution":  resolution,
        "date_format": "0",
        "range_from":  int(start_of_day.timestamp()),
        "range_to":    int(now.timestamp()),
        "cont_flag":   "1"
    }

    response       = fyers.history(data=nifty_data)
    historical_data = response['candles']
    df = pd.DataFrame(
        historical_data,
        columns=['date', 'open', 'high', 'low', 'close', 'volume']
    )
    df['date'] = (
        pd.to_datetime(df['date'], unit='s')
        .dt.tz_localize('UTC')
        .dt.tz_convert(pytz.timezone(timeZone))
    )
    df.set_index('date', inplace=True)
    return df


# ============================================================
# Log Data
# ============================================================
def log_trade(action, symbol, price):
    """
    Append trade details to a CSV log.

    Parameters:
        action (str): "BUY" or "SELL".
        symbol (str): Symbol.
        price (float): Execution price.
    """
    with open("trades_log.csv", "a") as file:
        file.write(f"{dt.datetime.now(pytz.timezone(timeZone))},{action},'RSI',{symbol},{price}\n")

# ============================================================
# Candlestick Class — WebSocket Event Manager
# ============================================================
class Candlestick:
    """
    Real-time market data manager.

    Owns the live OHLCV DataFrame and the RSIState.
    Receives WebSocket callbacks and delegates to update_live_data().
    """

    def __init__(self, data: pd.DataFrame, fyers: fyersModel.FyersModel, rsi_state: RSIState):
        self.data              = data
        self.fyers             = fyers
        self.rsi_state         = rsi_state
        self.last_total_volume: Optional[int] = None

        # Trading State Variables
        self.position = None     # LONG / SHORT / None
        self.sl = None           # Active stop loss
        self.tp = None           # Active take profit
        self.trigger = None      # Price level used for trailing logic

        # Prevents duplicate signal evaluation on the same candle
        self.last_evaluated_candle = None


    # ============================================================
    # Cleaner function
    # ============================================================    
    def _clear_position(self):
        self.position = None
        self.sl = None
        self.tp = None
        self.trigger = None
    
    def _get_quote(self) -> dict:
        """Fetch live bid/ask for the symbol."""
        return self.fyers.quotes(data={"symbols": symbol})['d'][0]['v']

    def onmessage(self, message: dict) -> None:
        """Handle an incoming tick from the Fyers WebSocket."""
        self.data, self.last_total_volume = update_live_data(
            data=self.data,
            message=message,
            last_total_volume=self.last_total_volume,
            rsi_state=self.rsi_state
        )
        # Show last few candles with live RSI
        print(self.data[['open', 'high', 'low', 'close', 'volume', 'rsi', 'ema_9', 'sma_9']].tail())

        # --- Guard Conditions ---
        if len(self.data) < 2:
            return
        
        ltp = message.get('ltp')
        if ltp is None:
            return

        # --- Last Closed Candle ---
        closed_candle = self.data.iloc[-2]
        closed_candle_time = self.data.index[-2]
        is_new_candle = closed_candle_time != self.last_evaluated_candle

        # --------------------------------------------------------
        # TICK-LEVEL Exit: Hard TP/SL (runs on EVERY tick)
        # --------------------------------------------------------
        if self.position == 'LONG' and (ltp >= self.tp or ltp <= self.sl):
            print(f"[EXIT LONG] Hard TP/SL hit at {ltp}")
            bid = self._get_quote()['bid']
            log_trade("Sell", symbol, bid)
            self._clear_position()

        elif self.position == 'SHORT' and (ltp <= self.tp or ltp >= self.sl):
            print(f"[EXIT SHORT] Hard TP/SL hit at {ltp}")
            ask = self._get_quote()['ask']
            log_trade("Buy", symbol, ask)
            self._clear_position()


        # --------------------------------------------------------
        # CANDLE-LEVEL Logic (runs only once per closed candle)
        # --------------------------------------------------------
        if not is_new_candle:
            return

        # Mark this candle as evaluated ONCE, at the top
        self.last_evaluated_candle = closed_candle_time

        rsi       = self.data['rsi'].iloc[-2]
        ema       = self.data['ema'].iloc[-2]
        sma       = self.data['sma'].iloc[-2]

        # ✅ Skip signal logic entirely if EMAs aren't ready yet
        if any(pd.isna(v) for v in [rsi, ema, sma]):
            return
        
        # --------------------------------------------------------
        # CANDLE-LEVEL Exit: Cross signals + Trailing SL
        # --------------------------------------------------------
        # Move TP and SL together by the same amount.
        #
        # Example:
        # Old SL = 100
        # Old TP = 120
        #
        # New SL = 105
        #
        # Risk locked in = +5
        # TP shifted to 125
        #
        # This preserves the original reward:risk structure
        # while protecting accumulated profit.
        if self.position == 'LONG':

            if ema < sma or rsi < 65:
                print(f"[EXIT LONG] rsi crossed below 70 {closed_candle_time}")
                bid = self._get_quote()['bid']
                log_trade("Sell", symbol, bid)
                self._clear_position()

            elif closed_candle['close'] > self.trigger:
                new_sl = closed_candle['low']
                if new_sl > self.sl:
                    diff    = new_sl - self.sl
                    self.tp += diff
                    self.sl  = new_sl
                    self.trigger = closed_candle['high']

        elif self.position == 'SHORT':

            if rsi > 35 or ema > sma:
                print(f"[EXIT SHORT] rsi crossed above 30 {closed_candle_time}")
                ask = self._get_quote()['ask']
                log_trade("Buy", symbol, ask)
                self._clear_position()

            elif closed_candle['close'] < self.trigger:
                new_sl = closed_candle['high']
                if new_sl < self.sl:
                    diff    = self.sl - new_sl
                    self.tp -= diff
                    self.sl  = new_sl
                    self.trigger = closed_candle['low']
        

        # --------------------------------------------------------
        # CANDLE-LEVEL Entry (only if flat after exit above)
        # --------------------------------------------------------
        if not self.position:

            # Long Entry Conditions:
            #
            # RSI crossed above 70
            #  -> bullish momentum is accelerating.
            if rsi > 65 and ema > sma:
                ask = self._get_quote()['ask']
                log_trade("Buy", symbol, ask)
                self.sl       = closed_candle['low']
                self.tp       = ask + (ask - self.sl) * 2
                self.trigger  = closed_candle['high']
                self.position = 'LONG'
                print(f"[BUY] at {ask} | SL: {self.sl} | TP: {self.tp}")

            # Short Entry Conditions:
            #
            # RSI crossed below 30
            #  -> bearish momentum is accelerating.
            elif rsi < 35 and ema < sma:
                bid = self._get_quote()['bid']
                log_trade("Sell", symbol, bid)
                self.sl       = closed_candle['high']
                self.tp       = bid - (self.sl - bid) * 2
                self.trigger  = closed_candle['low']
                self.position = 'SHORT'
                print(f"[SELL] at {bid} | SL: {self.sl} | TP: {self.tp}")


    def onerror(self, message: dict) -> None:
        print("WebSocket Error:", message)

    def onclose(self, message: dict) -> None:
        print("Connection closed:", message)

    def onopen(self) -> None:
        """Subscribe to symbol feed on connection."""
        fyersSocket.subscribe(symbols=[symbol], data_type="SymbolUpdate")
        fyersSocket.keep_running()


# ============================================================
# Entry Point
# ============================================================
if __name__ == "__main__":
    """
    Startup sequence
    ─────────────────
    1. Load access token
    2. Fetch today's historical candles
    3. Initialize RSI state from historical data  (TA-Lib used once here)
    4. Start WebSocket → incremental RSI from this point forward
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

    print("Connecting to live stream...")
    fyersSocket.connect()
