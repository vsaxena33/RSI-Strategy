"""
================================================================================
ROLLING MOVING AVERAGES ENGINE
================================================================================

This module provides lightweight, O(1) mathematical updates for running 
Exponential Moving Averages (EMA) and Simple Moving Averages (SMA) on live streams.
"""

# ============================================================
# Import
# ============================================================
import pandas as pd

# ============================================================
# Rolling EMA
# ============================================================
def rolling_ema(ltp: float, prev_ema: float, length: int) -> float:
    """
    Compute the live incremental EMA for the current running tick.

    Parameters
    ----------
    ltp : float
        Latest Traded Price (current tick close).
    prev_ema : float
        The finalized EMA value from the last closed candle.
    length : int
        Lookback period for the EMA window.

    Returns
    -------
    float
        The calculated live EMA value.
    """
    multiplier = 2 / (length + 1)
    new_ema = (ltp - prev_ema) * multiplier + prev_ema
    return new_ema

# ============================================================
# Rolling SMA
# ============================================================
def rolling_sma(df: pd.DataFrame, ltp: float, length: int) -> float:
    """
    Compute the finalized SMA for a brand new candle snapshot.

    Parameters
    ----------
    df : pd.DataFrame
        The historical DataFrame containing candle history up to the previous index.
    ltp : float
        Latest Traded Price forming the initial print of the new candle.
    length : int
        Lookback period for the SMA window.

    Returns
    -------
    float
        The calculated SMA value.
    """
    prev_sma     = df['sma_9'].iloc[-1]
    oldest_close = df['close'].iloc[-length]
    return prev_sma + (ltp - oldest_close) / length
