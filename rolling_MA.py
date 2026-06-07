# ============================================================
# Import
# ============================================================
import pandas as pd

def rolling_ema(ltp: float, prev_ema: float, length: int) -> float:
    multiplier = 2 / (length + 1)
    new_ema = (ltp - prev_ema) * multiplier + prev_ema
    return new_ema

def rolling_sma(df: pd.DataFrame, ltp: float, length: int) -> float:
    prev_sma     = df['sma_9'].iloc[-1]
    oldest_close = df['close'].iloc[-length]
    return prev_sma + (ltp - oldest_close) / length