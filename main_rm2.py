import os
import time
import json
import pandas as pd
import pandas_ta as ta 
from datetime import datetime, timezone
from dotenv import load_dotenv
from pocketoptionapi.stable_api import PocketOption
import pocketoptionapi.global_value as global_value
import oandapyV20
import oandapyV20.endpoints.instruments as instruments

# Load environment variables
load_dotenv()

# === Credentials ===
ACCESS_TOKEN = os.getenv("OANDA_TOKEN")
ACCOUNT_ID = os.getenv("OANDA_ID")
ssid = os.getenv("PO_SSID")
demo = True

# Bot Settings
min_payout = 60
period = 60
expiration = 60
INITIAL_AMOUNT = 1
MARTINGALE_LEVEL = 3

api = PocketOption(ssid, demo)
api.connect()
time.sleep(5)

def get_oanda_candles(pair, granularity="M1", count=500):
    try:
        client = oandapyV20.API(access_token=ACCESS_TOKEN)
        params = {"granularity": granularity, "count": count}
        r = instruments.InstrumentsCandles(instrument=pair, params=params)
        client.request(r)
        candles = r.response['candles']
        df = pd.DataFrame([{
            'time': c['time'],
            'open': float(c['mid']['o']),
            'high': float(c['mid']['h']),
            'low': float(c['mid']['l']),
            'close': float(c['mid']['c']),
        } for c in candles])
        df['time'] = pd.to_datetime(df['time'])
        return df
    except Exception as e:
        global_value.logger(f"[ERROR]: OANDA candle fetch failed for {pair} - {str(e)}", "ERROR")
        return None

def get_payout():
    try:
        d = json.loads(global_value.PayoutData)
        for pair in d:
            name = pair[1]
            payout = pair[5]
            asset_type = pair[3]
            is_active = pair[14]

            if not name.endswith("_otc") and asset_type == "currency" and is_active:
                if payout >= min_payout:
                    global_value.pairs[name] = {'payout': payout, 'type': asset_type}
                elif name in global_value.pairs:
                    del global_value.pairs[name]
        return True
    except Exception as e:
        global_value.logger(f"[ERROR]: Failed to parse payout data - {str(e)}", "ERROR")
        return False

def prepare_data(df):
    df = df[['time', 'open', 'high', 'low', 'close']]
    df.rename(columns={'time': 'timestamp'}, inplace=True)
    df.sort_values(by='timestamp', inplace=True)

    # Keep only SuperTrend and RSI calculations
    df['RSI'] = ta.rsi(df['close'], length=14)
    supert = ta.supertrend(df['high'], df['low'], df['close'], length=10, multiplier=3.0)
    df['SUPERT_10_3.0'] = supert['SUPERT_10_3.0']
    df['SUPERTd_10_3.0'] = supert['SUPERTd_10_3.0']
    
    # Calculate signal changes
    df['signal'] = df['SUPERTd_10_3.0'].diff()
    
    df.dropna(inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df

def detect_signal(df):
    # Check the last row for signal
    latest_index = len(df) - 1
    
    # Ensure we have enough data points
    if latest_index < 2:
        return None, None
    
    # Buy signal: trend changed from -1 to 1 (signal diff = 2)
    if df.loc[latest_index, 'signal'] == 2:
        decision = "call"
        global_value.logger("🟢 === BUY SIGNAL DETECTED | Decision: CALL", "INFO")
        return decision, df
    
    # Sell signal: trend changed from 1 to -1 (signal diff = -2)
    elif df.loc[latest_index, 'signal'] == -2:
        decision = "put"
        global_value.logger("🔴 === SELL SIGNAL DETECTED | Decision: PUT", "INFO")
        return decision, df
    
    return None, None

def perform_trade(amount, pair, action, expiration):
    result = api.buy(amount=amount, active=pair, action=action, expirations=expiration)
    trade_id = result[1]

    if result[0] is False or trade_id is None:
        global_value.logger("❗Trade failed to execute. Attempting reconnection...", "ERROR")
        api.disconnect()
        time.sleep(2)
        api.connect()
        return None

    time.sleep(expiration)
    return api.check_win(trade_id)

def martingale_strategy(pair, action, df):
    global current_profit

    # Print the last 5 rows of the dataframe when executing trade
    global_value.logger(f"📊 Data leading to trade decision:\n{df.tail(5).to_string()}", "INFO")

    amount = INITIAL_AMOUNT
    level = 1
    result = perform_trade(amount, pair, action, expiration)

    if result is None:
        return

    while result[1] == 'loose' and level < MARTINGALE_LEVEL:
        level += 1
        amount *= 2
        result = perform_trade(amount, pair, action, expiration)

        if result is None:
            return
        
    if result[1] != 'loose':
        global_value.logger("WIN - Resetting to base amount.", "INFO")
    else:
        global_value.logger("LOSS. Resetting.", "INFO")

def wait_until_next_candle(period_seconds=300, seconds_before=10):
    while True:
        now = datetime.now(timezone.utc)
        next_candle = ((now.timestamp() // period_seconds) + 1) * period_seconds
        if now.timestamp() >= next_candle - seconds_before:
            break
        time.sleep(0.1)

def wait_for_candle_start():
    while True:
        now = datetime.now(timezone.utc)
        if now.second == 0 and now.minute % (period // 60) == 0:
            break
        time.sleep(0.1)

def main_trading_loop():
    while True:
        global_value.logger("🔄 Starting new trading cycle...", "INFO")

        if not get_payout():
            global_value.logger("❗Failed to get payout data.", "ERROR")
            time.sleep(5)
            continue

        wait_until_next_candle(period_seconds=period, seconds_before=15)
        global_value.logger("🕒 15 seconds before candle. Preparing data and predictions...", "INFO")

        selected_pair = None
        selected_action = None
        trade_df = None

        # Log the pairs being checked
        global_value.logger(f"🔍 Checking {len(global_value.pairs)} pairs for signals...", "INFO")
        
        for pair in list(global_value.pairs.keys()):
            oanda_pair = pair[:3] + "_" + pair[3:]
            df = get_oanda_candles(oanda_pair)

            if df is None:
                global_value.logger(f"=={pair}== No data", "INFO")
                continue

            df = prepare_data(df)
            decision, signal_df = detect_signal(df)

            if decision:
                selected_pair = pair
                selected_action = decision
                trade_df = signal_df
                global_value.logger(f"=={pair}== Signal detected", "INFO")
                global_value.logger(f"✅ Selected {pair} for {decision.upper()} trade.", "INFO")
                break  # Stop at first valid signal
            else:
                global_value.logger(f"=={pair}== No signal", "INFO")

        wait_for_candle_start()

        if selected_pair and selected_action:
            global_value.logger(f"🚀 Executing trade on {selected_pair} - {selected_action.upper()}", "INFO")
            martingale_strategy(selected_pair, selected_action, trade_df)
        else:
            global_value.logger("⛔ No valid trading signal this cycle.", "INFO")

        # Optional: small pause before starting next cycle
        time.sleep(1)

if __name__ == "__main__":
    main_trading_loop()