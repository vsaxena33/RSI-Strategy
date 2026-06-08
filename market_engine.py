# ============================================================
# Import
# ============================================================
import pandas as pd
from fyers_apiv3 import fyersModel
from fyers_apiv3.FyersWebsocket import data_ws
from typing import Optional
from candle_engine import update_live_data
from RSI import RSIState
from log_trade import log_trade

# ============================================================
# Configuration
# ============================================================
symbol     = 'NSE:RELIANCE-EQ'

# ============================================================
# Candlestick Class — WebSocket Event Manager
# ============================================================
class Candlestick:
    """
    Candlestick acts as the central coordinator of the trading engine.

    Responsibilities
    ----------------
    1. Receive WebSocket ticks.
    2. Update OHLCV candles.
    3. Maintain rolling indicators.
    4. Manage trading state.
    5. Execute strategy logic.
    6. Log trades.

    The class itself does not calculate RSI.

    Indicator calculations are delegated to dedicated modules,
    allowing indicators to be developed and tested independently.
    """

    def __init__(self, data: pd.DataFrame, fyers: fyersModel.FyersModel, rsi_state: RSIState):
        self.data              = data
        self.fyers             = fyers
        self.rsi_state         = rsi_state
        self.fyersSocket: data_ws.FyersDataSocket = None
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
        ema       = self.data['ema_9'].iloc[-2]
        sma       = self.data['sma_9'].iloc[-2]

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
        self.fyersSocket.subscribe(symbols=[symbol], data_type="SymbolUpdate")
        self.fyersSocket.keep_running()
