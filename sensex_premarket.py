import os
import sys
import time
import datetime
import requests
import pyotp
import base64
import numpy as np
from colorama import Fore, Style, init
from urllib.parse import urlparse, parse_qs
from fyers_apiv3 import fyersModel
from fyers_apiv3.FyersWebsocket import data_ws

init(autoreset=True)

# ---------------------------------------------------------
# ૧. GitHub Secrets / Environment Variables
# ---------------------------------------------------------
CLIENT_ID          = os.getenv("FYERS_CLIENT_ID")
USER_PIN           = os.getenv("FYERS_PIN")
TOTP_KEY           = os.getenv("FYERS_TOTP_KEY")
APP_ID             = os.getenv("FYERS_APP_ID") or os.getenv("APP_ID")
SECRET_ID          = os.getenv("FYERS_SECRET_ID") or os.getenv("SECRET_KEY")
REDIRECT_URI       = os.getenv("FYERS_REDIRECT_URI", "https://trade.fyers.in/api-login/redirect-uri/index.html")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

target_symbol = "BSE:SENSEX-INDEX"
pre_market_ticks = []
open_price_900 = None  # Stores the 09:00:01 AM Open Tick

def validate_env_vars():
    missing = []
    if not CLIENT_ID: missing.append("FYERS_CLIENT_ID")
    if not USER_PIN: missing.append("FYERS_PIN")
    if not TOTP_KEY: missing.append("FYERS_TOTP_KEY")
    if not APP_ID: missing.append("FYERS_APP_ID")
    if not SECRET_ID: missing.append("FYERS_SECRET_ID")

    if missing:
        print(f"{Fore.RED}[-] Missing Secrets: {', '.join(missing)}{Style.RESET_ALL}")
        os._exit(1)

# ---------------------------------------------------------
# ૨. Fixed Fyers Automated Login
# ---------------------------------------------------------
def get_automated_token():
    validate_env_vars()
    print(f"{Fore.CYAN}[*] Fyers બેકગ્રાઉન્ડ લોગિન શરૂ થઈ રહ્યું છે...{Style.RESET_ALL}")
    try:
        session = requests.Session()
        b64_encode = lambda s: base64.b64encode(str(s).encode()).decode()

        # Step 1: Send OTP
        payload_otp = {"fy_id": b64_encode(CLIENT_ID), "app_id": "2"}
        res_otp = session.post("https://api-t2.fyers.in/vagator/v2/send_login_otp_v2", json=payload_otp).json()
        request_key = res_otp.get("request_key") or res_otp.get("data", {}).get("request_key")
        if not request_key:
            print(f"{Fore.RED}[-] OTP Request Failed:{Style.RESET_ALL}", res_otp)
            os._exit(1)

        # Step 2: Verify TOTP
        time_remaining = 30 - (int(time.time()) % 30)
        if time_remaining < 4:
            time.sleep(time_remaining + 1)

        totp_code = pyotp.TOTP(TOTP_KEY).now()
        payload_verify = {"request_key": request_key, "otp": totp_code}
        res_verify = session.post("https://api-t2.fyers.in/vagator/v2/verify_otp", json=payload_verify).json()
        request_key_v2 = res_verify.get("request_key") or res_verify.get("data", {}).get("request_key")
        if not request_key_v2:
            print(f"{Fore.RED}[-] TOTP Verification Failed:{Style.RESET_ALL}", res_verify)
            os._exit(1)

        # Step 3: Verify PIN
        payload_pin = {
            "request_key": request_key_v2, 
            "identity_type": "pin", 
            "identifier": b64_encode(USER_PIN)
        }
        res_pin = session.post("https://api-t2.fyers.in/vagator/v2/verify_pin_v2", json=payload_pin).json()
        access_token_v2 = res_pin.get("data", {}).get("token") or res_pin.get("data", {}).get("access_token")
        if not access_token_v2:
            print(f"{Fore.RED}[-] PIN Verification Failed:{Style.RESET_ALL}", res_pin)
            os._exit(1)

        # Step 4: OAuth Token Generation & Auth Code Extraction
        clean_app_id = APP_ID.split("-")[0] if "-" in APP_ID else APP_ID
        headers = {"Authorization": f"Bearer {access_token_v2}", "Content-Type": "application/json"}
        payload_oauth = {
            "fyers_id": CLIENT_ID,
            "app_id": clean_app_id,
            "redirect_uri": REDIRECT_URI,
            "appType": "100",
            "code_challenge": "",
            "state": "sample_state",
            "scope": "",
            "nonce": "",
            "response_type": "code",
            "create_cookie": True
        }
        res_oauth = session.post("https://api-t1.fyers.in/api/v3/token", json=payload_oauth, headers=headers).json()
        print(f"{Fore.YELLOW}[*] OAuth Response Received:{Style.RESET_ALL}", res_oauth)

        # Extract redirect URL containing auth_code
        redirect_url = res_oauth.get("Url")
        if redirect_url:
            parsed_url = urlparse(redirect_url)
            query_params = parse_qs(parsed_url.query)
            auth_code_list = query_params.get("auth_code")

            if auth_code_list:
                auth_code = auth_code_list[0]

                # Step 5: Exchange auth_code for final access_token using Fyers SessionModel
                session_model = fyersModel.SessionModel(
                    client_id=APP_ID,  # Full App ID with type e.g., 'ABCDE-100'
                    secret_key=SECRET_ID,
                    redirect_uri=REDIRECT_URI,
                    response_type="code",
                    grant_type="authorization_code"
                )
                session_model.set_token(auth_code)
                token_response = session_model.generate_token()
                print(f"{Fore.YELLOW}[*] Token Generation Response:{Style.RESET_ALL}", token_response)

                access_token = token_response.get("access_token")
                if access_token:
                    print(f"{Fore.GREEN}[+] Fyers ઓટો-લોગિન સફળ!{Style.RESET_ALL}")
                    return access_token
                else:
                    print(f"{Fore.RED}[-] Access Token missing in response!{Style.RESET_ALL}")
                    os._exit(1)

        print(f"{Fore.RED}[-] Redirect URL extraction failed:{Style.RESET_ALL}", res_oauth)
        os._exit(1)

    except Exception as err:
        print(f"{Fore.RED}[-] લોગિન ક્રેશ: {err}{Style.RESET_ALL}")
        os._exit(1)

# ---------------------------------------------------------
# ૩. Telegram Alert & Strategy Calculations
# ---------------------------------------------------------
def send_telegram_alert(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"{Fore.YELLOW}[!] Telegram Tokens નથી મળ્યા.{Style.RESET_ALL}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload)
        print(f"{Fore.GREEN}[+] Telegram માં સક્સેસફુલ મેસેજ સેન્ડ થયો.{Style.RESET_ALL}")
    except Exception as e:
        print(f"{Fore.RED}[-] Telegram Alert માં એરર: {e}{Style.RESET_ALL}")

def calculate_and_send_strategy(ticks):
    if not ticks:
        send_telegram_alert("⚠️ *BSE Sensex Alert*: Pre-market ticks capture થતા નથી!")
        return

    open_price = open_price_900 if open_price_900 else ticks[0]
    close_price = ticks[-1]  

    abs_high = max(ticks)
    abs_low = min(ticks)

    p_high = float(np.percentile(ticks, 95))
    p_low  = float(np.percentile(ticks, 5))

    filtered_ticks = [t for t in ticks if p_low <= t <= p_high]
    if not filtered_ticks:
        filtered_ticks = ticks

    mean = float(np.mean(filtered_ticks))
    std_dev = float(np.std(filtered_ticks))

    if std_dev < (mean * 0.001):
        std_dev = mean * 0.0015

    u1sd = round(mean + std_dev, 2)
    l1sd = round(mean - std_dev, 2)
    u2sd = round(mean + (2 * std_dev), 2)
    l2sd = round(mean - (2 * std_dev), 2)

    high_mid = round((p_high + mean) / 2.0, 2)
    low_mid  = round((mean + p_low) / 2.0, 2)
    range_span = round(p_high - p_low, 2)

    if range_span > 0:
        mean_pos = (mean - p_low) / range_span
        if mean_pos >= 0.65:
            bias = "🟢 BULLISH (Mean near Pre-Open High)"
        elif mean_pos <= 0.35:
            bias = "🔴 BEARISH (Mean near Pre-Open Low)"
        else:
            bias = "🟡 NEUTRAL (Balanced Range)"
    else:
        bias = "🟡 NEUTRAL / FLUSH OPEN"

    spread_pct = (range_span / mean) * 100
    trap_risk = "⚠️ HIGH TRAP RISK (Narrow Range)" if spread_pct < 0.25 else "✅ NORMAL RANGE (Standard Volatility)"

    msg = f"""🚨 *BSE SENSEX MORNING TRADING PLAN* 🚨
📅 *Date:* {datetime.datetime.now().strftime('%d-%m-%Y')}

⏰ *Pre-Market Executed Prices:*
• *09:00:01 AM Open:* `{open_price:.2f}`
• *Pre-Open Close:* `{close_price:.2f}`

📊 *Key Pre-Open Levels (Spike Filtered):*
• *Core High (95th %):* `{p_high:.2f}` _(Abs Max: {abs_high:.2f})_
• *Upper Mid (H-Pivot):* `{high_mid:.2f}`
• *Pre-Open Mean:* `{mean:.2f}`
• *Lower Mid (L-Pivot):* `{low_mid:.2f}`
• *Core Low (5th %):* `{p_low:.2f}` _(Abs Min: {abs_low:.2f})_
• *Pre-Open Range:* `{range_span:.2f} pts`

📈 *Normal Distribution Zones (True SD: {std_dev:.2f} pts):*
• *+2 SD (Extreme Resistance):* `{u2sd:.2f}`
• *+1 SD (Upper Boundary):* `{u1sd:.2f}`
• *Mean (Pivot Zone):* `{mean:.2f}`
• *-1 SD (Lower Boundary):* `{l1sd:.2f}`
• *-2 SD (Extreme Support):* `{l2sd:.2f}`

🔍 *Market Environment:*
• *Bias:* {bias}
• *Structure:* {trap_risk}

---

💡 *EXECUTION PLAYBOOK (09:15 AM)*

1️⃣ *Reversal Setup (Mean Reversion):*
   • *SHORT / PUT:* Price reaches `{u1sd:.2f}` or `{u2sd:.2f}` AND forms a 5-min Bearish Rejection Candle.
   • *LONG / CALL:* Price reaches `{l1sd:.2f}` or `{l2sd:.2f}` AND forms a 5-min Bullish Rejection Candle.

2️⃣ *Breakout / Trap Setup:*
   • If price breaks `{p_high:.2f}` or `{p_low:.2f}` on the first 5-min candle, **DO NOT ENTER IMMEDIATELY**.
   • Wait for a pullback/retest of the broken level to confirm Support/Resistance.
"""
    send_telegram_alert(msg)

# ---------------------------------------------------------
# ૪. WebSocket Data Stream & Main Execution
# ---------------------------------------------------------
def on_message(message):
    global pre_market_ticks, open_price_900
    ltp = None
    if isinstance(message, dict) and message.get("symbol") == target_symbol:
        ltp = message.get("ltp") or message.get("lp")
    elif isinstance(message, list):
        for item in message:
            if isinstance(item, dict) and item.get("symbol") == target_symbol:
                ltp = item.get("ltp") or item.get("lp")
                break

    if ltp and ltp > 0:
        if open_price_900 is None:
            open_price_900 = ltp
        pre_market_ticks.append(ltp)

def on_error(msg): pass
def on_close(msg): pass

def on_open():
    fyers_ws.subscribe(symbols=[target_symbol], data_type="SymbolUpdate")

if __name__ == "__main__":
    access_token = get_automated_token()
    ws_access_token = f"{APP_ID}:{access_token}"

    fyers_ws = data_ws.FyersDataSocket(
        access_token=ws_access_token,
        log_path="",
        write_to_file=False,
        reconnect=False,
        on_connect=on_open,
        on_close=on_close,
        on_error=on_error,
        on_message=on_message
    )

    fyers_ws.connect()
    print(f"{Fore.CYAN}[*] Sensex Pre-market Ticks કેપ્ચર થઈ રહ્યા છે... (8.5 મિનિટ માટે){Style.RESET_ALL}")

    start_time = time.time()
    while time.time() - start_time < 510:
        time.sleep(1)

    try:
        fyers_ws.close_connection()
    except Exception:
        pass

    print(f"{Fore.GREEN}[+] Pre-market સેશન પૂરૂં. Ticks captured: {len(pre_market_ticks)}{Style.RESET_ALL}")

    calculate_and_send_strategy(pre_market_ticks)

    print(f"{Fore.GREEN}[+] Process complete. Force exiting...{Style.RESET_ALL}")
    os._exit(0)
