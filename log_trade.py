"""
================================================================================
TRADE LOGGING COMPONENT
================================================================================

Provides automated IO appending tools to output standardized execution sheets.
"""

# ============================================================
# Import
# ============================================================
import datetime as dt
import pytz

# ============================================================
# Configuration
# ============================================================
timeZone   = 'Asia/Kolkata'

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
        file.write(f"{dt.datetime.now(pytz.timezone(timeZone))},{action},{symbol},{price}\n")
