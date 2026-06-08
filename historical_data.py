"""
================================================================================
HISTORICAL DATA SEEDING UTILITIES
================================================================================

Handles initialization pipeline querying, fetching structural backlogs from
Fyers Intraday API servers for data priming.
"""

# ============================================================
# Import
# ============================================================
from fyers_apiv3 import fyersModel
import pandas as pd
import datetime as dt
import pytz

# ============================================================
# Configuration
# ============================================================
symbol     = 'NSE:RELIANCE-EQ'
timeZone   = 'Asia/Kolkata'
resolution = "1"

# ============================================================
# Historical Data Fetching
# ============================================================
def fetch_historical_data(fyers: fyersModel.FyersModel) -> pd.DataFrame:
    """
    Fetch intraday OHLCV candles from market open to now.

    Parameters
    ----------
    fyers      : Authenticated FyersModel instance.
    symbol     : Fyers symbol string (e.g. 'NSE:RELIANCE-EQ').
    resolution : Candle timeframe in minutes as a string (default '1').

    Returns
    -------
    pd.DataFrame
        OHLCV DataFrame indexed by timezone-aware timestamps.
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
