import os
import sys
import time
import datetime
import requests
import pyotp
import base64
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
APP_ID             = os.getenv("APP_ID") or os.getenv("FYERS_APP_ID")
SECRET_ID          = os.getenv("SECRET_KEY") or os.getenv("FYERS_SECRET_ID")
REDIRECT_URI       = os.getenv("FYERS_REDIRECT_URI", "https://trade.fyers.in/api-login/default-redirect")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

target_symbol = "BSE:SENSEX-INDEX"
pre_market_ticks = []

# Secrets ચકાસણી
def validate_env_vars():
    missing = []
    if not CLIENT_ID: missing.append("FYERS_CLIENT_ID")
    if not USER_PIN: missing.append("FYERS_PIN")
    if not TOTP_KEY: missing.append("FYERS_TOTP_KEY")
    if not APP_ID: missing.append("APP_ID / FYERS_APP_ID")
    if not SECRET_ID: missing.append("SECRET_KEY / FYERS_SECRET_ID")
    
    if missing:
        print(f"{Fore.RED}[-] નીચેના Secrets મળ્યા નથી: {', '.join(missing)}{Style.RESET_ALL}")
        sys.exit(1)

# ---------------------------------------------------------
# ૨. Fyers Automated Login Logic (Internal)
# ---------------------------------------------------------
def get_automated_token():
    validate_env_vars()
    print(f"{Fore.CYAN}[*] Fyers બેકગ્રાઉન્ડ લોગિન ઓટોમેશન શરૂ થઈ રહ્યું છે...{Style.RESET_ALL}")
    try:
        session = requests.Session()
        b64_encode = lambda s: base64.b64encode(str(s).encode()).decode()
        
        # Step 1: Send OTP request
        payload_otp = {"fy_id": b64_encode(CLIENT_ID), "app_id": "2"}
        res_otp = session.post("https://api-t2.fyers.in/vagator/v2/send_login_otp_v2", json=payload_otp).json()
        request_key = res_otp.get("request_key") or res_otp.get("data", {}).get("request_key")
        if not request_key:
            print(f"{Fore.RED}[-] OTP Request Failed:{Style.RESET_ALL}", res_otp)
            sys.exit(1)
            
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
            sys.exit(1)
            
        # Step 3: Verify PIN
        payload_pin = {
            "request_key": request_key_v2, 
            "identity_type": "pin", 
            "identifier": b64_encode(USER_PIN)
        }
        res_pin = session.post("https://api-t2.fyers.in/vagator/v2/verify_pin_v2", json=payload_pin).json()
        access_token_v2 = res_pin.get("data", {}).get("access_token") if isinstance(res_pin.get("data"), dict) else res_pin.get("access_token")
        if not access_token_v2:
            print(f"{Fore.RED}[-] PIN Verification Failed:{Style.RESET_ALL}", res_pin)
            sys.exit(1)
            
        # Step 4: OAuth Token Generation
        app_session = fyersModel.SessionModel(
            client_id=APP_ID, secret_key=SECRET_ID, redirect_uri=REDIRECT_URI, 
            response_type="code", grant_type="authorization_code"
        )
        headers = {"Authorization": f"Bearer {access_token_v2}", "Content-Type": "application/json"}
        payload_oauth = {
            "fyers_id": CLIENT_ID,
            "app_id": APP_ID.split("-")[0] if "-" in APP_ID else APP_ID,
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
        
        if res_oauth.get("s") == "ok" and "data" in res_oauth and "auth" in res_oauth["data"]:
            auth_code = res_oauth["data"]["auth"]
        else:
            redirect_target = res_oauth.get("Url") or res_oauth.get("data", {}).get("Url")
            auth_code = parse_qs(urlparse(redirect_target).query).get("auth_code", [""])[0]
            
        app_session.set_token(auth_code)
        token_response = app_session.generate_token()
        print(f"{Fore.GREEN}[+] Fyers ઓટો-લોગિન સફળ રહ્યું!{Style.RESET_ALL}")
        return token_response.get("access_token")
        
    except Exception as err:
        print(f"{Fore.RED}[-] લોગિન પ્રોસેસ ક્રેશ: {err}{Style.RESET_ALL}")
        sys.exit(1)

# ---------------------------------------------------------
# ૩. Telegram Alert & Strategy Calculations
# ---------------------------------------------------------
def send_telegram_alert(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"{Fore.YELLOW}[!] Telegram Tokens નથી મળ્યા. સ્કીપ કરાય છે.{Style.RESET_ALL}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload)
        print(f"{Fore.GREEN}[+] Telegram માં સફળતાપૂર્વક મેસેજ મોકલાયો.{Style.RESET_ALL}")
    except Exception as e:
        print(f"{Fore.RED}[-] Telegram Alert મોકલવામાં ભૂલ: {e}{Style.RESET_ALL}")

def calculate_and_send_strategy(ticks):
    if not ticks:
        send_telegram_alert("⚠️ *BSE Sensex Alert*: Pre-market ticks capture થતા નથી!")
        return
    
    pm_high = max(ticks)
    pm_low = min(ticks)
    pm_close = ticks[-1]
    
    # Normal Distribution (પ્રમાણ્ય વિતરણ)
    mean = (pm_high + pm_low + pm_close) / 3.0
    range_pm = pm_high - pm_low
    sigma = range_pm / 2.0 if range_pm > 0 else 10.0
    
    p3sd, m3sd = mean + (3 * sigma), mean - (3 * sigma)
    p2sd, m2sd = mean + (2 * sigma), mean - (2 * sigma)
    p1sd, m1sd = mean + (1 * sigma), mean - (1 * sigma)
    
    msg = f"""📊 *BSE SENSEX PRE-MARKET STRATEGY*
📅 Date: {datetime.datetime.now().strftime('%d-%m-%Y')}

🔴 *Pre-Market Data (09:00 - 09:08)*
• High: `{pm_high:.2f}`
• Low: `{pm_low:.2f}`
• Close: `{pm_close:.2f}`

📈 *Normal Distribution Levels (પ્રમાણ્ય વિતરણ)*
• *+3SD (Breakout Target):* `{p3sd:.2f}`
• *+2SD (Sell / Reversal):* `{p2sd:.2f}`
• *+1SD (Upper Boundary):* `{p1sd:.2f}`
• 🎯 *Mean (Pivot Zone):* `{mean:.2f}`
• *-1SD (Lower Boundary):* `{m1sd:.2f}`
• *-2SD (Buy / Reversal):* `{m2sd:.2f}`
• *-3SD (Breakout Target):* `{m3sd:.2f}`

💡 *Today's Execution Plan:*
- Look for **Mean Reversion** at -2SD (Buy) or +2SD (Sell) on 5-min candle confirmation.
- **Breakout Trade** if 9:15 candle closes above +2SD or below -2SD.
"""
    send_telegram_alert(msg)

# ---------------------------------------------------------
# ૪. WebSocket Data Stream & Main Execution
# ---------------------------------------------------------
def on_message(message):
    global pre_market_ticks
    ltp = None
    if isinstance(message, dict) and message.get("symbol") == target_symbol:
        ltp = message.get("ltp") or message.get("lp")
    elif isinstance(message, list):
        for item in message:
            if isinstance(item, dict) and item.get("symbol") == target_symbol:
                ltp = item.get("ltp") or item.get("lp")
                break

    if ltp and ltp > 0:
        pre_market_ticks.append(ltp)

def on_error(msg): pass
def on_close(msg): pass
def on_open():
    fyers_ws.subscribe(symbols=[target_symbol], data_type="SymbolUpdate")
    fyers_ws.keep_running()

if __name__ == "__main__":
    access_token = get_automated_token()
    ws_access_token = f"{APP_ID}:{access_token}"

    fyers_ws = data_ws.FyersDataSocket(
        access_token=ws_access_token,
        log_path="",
        write_to_file=False,
        reconnect=True,
        on_connect=on_open,
        on_close=on_close,
        on_error=on_error,
        on_message=on_message
    )

    fyers_ws.connect()
    print(f"{Fore.CYAN}[*] Sensex Pre-market Ticks કેપ્ચર થઈ રહ્યા છે... (8.5 મિનિટ માટે){Style.RESET_ALL}")

    # 510 સેકન્ડ (8.5 મિનિટ) માટે રન થશે (અંદાજે 09:08:30 સુધી)
    start_time = time.time()
    while time.time() - start_time < 510:
        time.sleep(1)

    fyers_ws.close_connection()
    print(f"{Fore.GREEN}[+] Pre-market સેશન સમાપ્ત. Ticks captured: {len(pre_market_ticks)}{Style.RESET_ALL}")
    
    # ગણતરી કરીને Telegram પર મોકલો
    calculate_and_send_strategy(pre_market_ticks)
    sys.exit(0)
  
