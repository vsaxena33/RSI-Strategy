# ============================================================
# Import
# ============================================================
import pandas as pd
from typing import Optional
from RSI import RSIState, update_rsi
import rolling_MA
import numpy as np

# ============================================================
# Configuration
# ============================================================
timeZone   = 'Asia/Kolkata'
resolution = "1"
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
    # We are still inside the currently forming candle.
    #
    # Example:
    #
    # 10:15:01 -> Price = 100
    # 10:15:05 -> Price = 101
    # 10:15:10 -> Price = 99
    #
    # The candle is NOT closed yet.
    #
    # Therefore:
    #
    # • Update OHLCV
    # • Update temporary RSI
    # • Do NOT commit Wilder state
    #
    # This branch may execute thousands of times before
    # the candle closes.
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
            data.iloc[-1, COL_EMA9] = rolling_MA.rolling_ema(ltp, prev_ema_9, EMA_PERIOD) if pd.notna(prev_ema_9) else np.nan
            # For SMA mid-candle: slide against the base snapshot taken at candle open
            # We reuse the same oldest_close that was used when this candle was created.
            # The simplest correct approach: recompute from prev_sma + (ltp - oldest_close)/length
            # oldest_close is at position -(SMA_PERIOD) relative to the last CLOSED candle (iloc[-2])
            if pd.notna(prev_sma_9) and len(data) > SMA_PERIOD:
                oldest_close = data['close'].iloc[-(SMA_PERIOD + 1)]  # drops out of window for this candle
                data.iloc[-1, COL_SMA9] = prev_sma_9 + (ltp - oldest_close) / SMA_PERIOD
            else:
                data.iloc[-1, COL_SMA9] = np.nan

    # ── CLOSE previous candle and CREATE new one ──────────
    # The previous candle has now closed.
    #
    # Steps:
    #
    # 1. Commit previous candle RSI contribution.
    # 2. Advance Wilder averages.
    # 3. Create new candle.
    # 4. Derive temporary RSI for the new candle.
    #
    # From this point forward, the new candle becomes the
    # active live candle receiving tick updates.
    else:
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
 
        new_ema_9 = rolling_MA.rolling_ema(ltp, prev_ema_9, EMA_PERIOD) if pd.notna(prev_ema_9) else np.nan
 
        # rolling_sma needs the pre-append DataFrame and at least SMA_PERIOD+1 candles
        if pd.notna(prev_sma_9) and len(data) >= SMA_PERIOD + 1:
            new_sma_9 = rolling_MA.rolling_sma(data, ltp, SMA_PERIOD)
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
