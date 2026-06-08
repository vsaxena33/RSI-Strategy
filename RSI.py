"""
================================================================================
ROLLING RSI ENGINE
================================================================================

This module implements a production-grade rolling Relative Strength Index (RSI)
using Wilder's smoothing method.

Unlike traditional implementations that recalculate RSI across the entire price
history whenever a new candle arrives, this implementation maintains a compact
state object and updates RSI incrementally in O(1) time.

Design Philosophy
-----------------
The engine separates RSI state into two layers:

1. Committed State
   Represents the last fully closed candle.

2. Temporary State
   Represents the currently forming candle.

This architecture allows thousands of live tick updates to be processed
without mutating committed RSI values until the candle is finalized.

Benefits
--------
* O(1) update complexity
* O(1) memory growth
* No historical rescans
* No TA-Lib calls after initialization
* Suitable for real-time algorithmic trading systems

Workflow
--------
Historical Candles
        ↓
TA-Lib Initialization
        ↓
Wilder State Recovery
        ↓
Live Tick Updates
        ↓
Temporary RSI Calculation
        ↓
Candle Close
        ↓
State Commitment

Author: Vaibhav Saxena
================================================================================
"""


# ============================================================
# Import
# ============================================================
from dataclasses import dataclass
import numpy as np
import pandas as pd
import talib as ta

# ============================================================
# Configuration
# ============================================================
RSI_PERIOD = 28      # Standard Wilder period; change freely

# ============================================================
# RSI State Dataclass
# ============================================================
@dataclass
class RSIState:
    """
    Stores all state required for rolling RSI calculations.

    Attributes
    ----------
    period : int
        RSI lookback period.

    committed_avg_gain : float
        Wilder-smoothed average gain from the most recently
        closed candle.

    committed_avg_loss : float
        Wilder-smoothed average loss from the most recently
        closed candle.

    committed_close : float
        Closing price of the most recently closed candle.

    current_rsi : float
        RSI value currently displayed on the chart.

    current_avg_gain : float
        Temporary average gain derived from the live candle.

    current_avg_loss : float
        Temporary average loss derived from the live candle.

    is_ready : bool
        Indicates whether sufficient historical data exists
        to calculate RSI.
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
    Relative Strength (RS)
    RS = AvgGain / AvgLoss
    RSI converts RS into a bounded oscillator:
    RSI = 100 - 100 / (1 + RS)

    Edge cases:
      avg_loss == 0 and avg_gain >  0  →  pure uptrend  →  RSI = 100 (extremely bullish)
      avg_loss == 0 and avg_gain == 0  →  flat market   →  RSI = 50  (neutral)
      avg_gain == 0 and avg_loss >  0  →  pure downtrend→  RSI = 0   (extremely bearish)

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
    # IMPORTANT:
    #
    # RSI is always calculated relative to the last CLOSED candle.
    #
    # Example:
    #
    # Closed Candle Close = 100
    #
    # Tick 1 -> 101
    # Tick 2 -> 102
    # Tick 3 -> 99
    #
    # Each tick recomputes RSI from the same committed state:
    #
    # committed_avg_gain
    # committed_avg_loss
    # committed_close
    #
    # This prevents cumulative distortion during candle formation
    # and guarantees that only the final candle close affects
    # committed RSI values.
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

    # Once the candle closes, temporary values become permanent.
    #
    # At this point the live candle becomes part of market history,
    # so its gain/loss contribution must be incorporated into the
    # committed Wilder averages.
    #
    # This happens exactly once per candle.
    if candle_closed:
        # ── Promote temporary → committed ─────────────────────
        # This happens exactly ONCE per closed candle, not per tick.
        state.committed_avg_gain = temp_avg_gain
        state.committed_avg_loss = temp_avg_loss
        state.committed_close    = current_close

    return temp_rsi
