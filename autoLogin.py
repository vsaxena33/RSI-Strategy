import json
import requests
import pyotp
import hashlib
from urllib import parse
import sys
import credentials as cd

# This script will only work if TOTP is enabled. 
# You can enable TOTP using this link: https://myaccount.fyers.in/ManageAccount >> External 2FA TOTP >> click on "Enable".

# Client Information (ENTER YOUR OWN INFO HERE! Data varies from users and app types)
CLIENT_ID = cd.user_name        # Your Fyers Client ID
PIN = cd.pin                    # User pin for Fyers account
APP_ID = cd.client_id           # App ID from MyAPI dashboard (https://myapi.fyers.in/dashboard). The format is appId-appType. 
# Example: YIXXXXXSE-100. In this code, YIXXXXXSE is the APP_ID and 100 is the APP_TYPE
APP_TYPE = "100"
APP_SECRET = cd.secret_key      # App Secret from myapi dashboard (https://myapi.fyers.in/dashboard)
TOTP_SECRET_KEY = cd.totp_key   # TOTP secret key, copy the secret while enabling TOTP.

REDIRECT_URI = cd.redirect_uri  # Redirect URL from the app

# NOTE: Do not share these secrets with anyone.


# API endpoints
BASE_URL = "https://api-t2.fyers.in/vagator/v2"
BASE_URL_2 = "https://api-t1.fyers.in/api/v3"
URL_VERIFY_CLIENT_ID = BASE_URL + "/send_login_otp"
URL_VERIFY_TOTP = BASE_URL + "/verify_otp"
URL_VERIFY_PIN = BASE_URL + "/verify_pin"
URL_TOKEN = BASE_URL_2 + "/token"
URL_VALIDATE_AUTH_CODE = BASE_URL_2 + "/validate-authcode"

SUCCESS = 1
ERROR = -1

def verify_client_id(client_id):
    try:
        payload = {
            "fy_id": client_id,
            "app_id": "2"
        }

        result_string = requests.post(url=URL_VERIFY_CLIENT_ID, json=payload)
        # print("result_string : ", result_string.text)
        if result_string.status_code != 200:
            return [ERROR, result_string.text]

        result = json.loads(result_string.text)
        request_key = result["request_key"]

        return [SUCCESS, request_key]
    
    except Exception as e:
        return [ERROR, e]
    

def generate_totp(secret):
    try:
        generated_totp = pyotp.TOTP(secret).now()
        return [SUCCESS, generated_totp]
    
    except Exception as e:
        return [ERROR, e]


def verify_totp(request_key, totp):
    try:
        payload = {
            "request_key": request_key,
            "otp": totp
        }

        result_string = requests.post(url=URL_VERIFY_TOTP, json=payload)
        if result_string.status_code != 200:
            return [ERROR, result_string.text]

        result = json.loads(result_string.text)
        request_key = result["request_key"]

        return [SUCCESS, request_key]
    
    except Exception as e:
        return [ERROR, e]


def verify_PIN(request_key, pin):
    try:
        payload = {
            "request_key": request_key,
            "identity_type": "pin",
            "identifier": pin
        }

        result_string = requests.post(url=URL_VERIFY_PIN, json=payload)
        if result_string.status_code != 200:
            return [ERROR, result_string.text]
    
        result = json.loads(result_string.text)
        access_token = result["data"]["access_token"]

        return [SUCCESS, access_token]
    
    except Exception as e:
        return [ERROR, e]


def token(client_id, app_id, redirect_uri, app_type, access_token):
    try:
        payload = {
            "fyers_id": client_id,
            "app_id": app_id,
            "redirect_uri": redirect_uri,
            "appType": app_type,
            "code_challenge": "",
            "state": "sample_state",
            "scope": "",
            "nonce": "",
            "response_type": "code",
            "create_cookie": True
        }
        headers={'Authorization': f'Bearer {access_token}'}

        result_string = requests.post(
            url=URL_TOKEN, json=payload, headers=headers
        )

        if result_string.status_code != 308:
            return [ERROR, result_string.text]

        result = json.loads(result_string.text)
        url = result["Url"]
        auth_code = parse.parse_qs(parse.urlparse(url).query)['auth_code'][0]

        return [SUCCESS, auth_code]
    
    except Exception as e:
        return [ERROR, e]


def sha256_hash(appId, appType, appSecret):
    message = f"{appId}-{appType}:{appSecret}"
    message = message.encode()
    sha256_hash = hashlib.sha256(message).hexdigest()
    return sha256_hash


def validate_authcode(auth_code):
    try:
        app_id_hash = sha256_hash(appId=APP_ID, appType=APP_TYPE, appSecret=APP_SECRET)
        payload = {
            "grant_type": "authorization_code",
            "appIdHash": app_id_hash,
            "code": auth_code,
        }

        result_string = requests.post(url=URL_VALIDATE_AUTH_CODE, json=payload)
        if result_string.status_code != 200:
            return [ERROR, result_string.text]

        result = json.loads(result_string.text)
        access_token = result["access_token"]

        return [SUCCESS, access_token]
    
    except Exception as e:
        return [ERROR, e]


def main():
    # Step 1 - Retrieve request_key from verify_client_id Function
    verify_client_id_result = verify_client_id(client_id=CLIENT_ID)
    if verify_client_id_result[0] != SUCCESS:
        print(f"verify_client_id failure - {verify_client_id_result[1]}")
        sys.exit()
    else:
        print("verify_client_id success")

    # Step 2 - Generate totp
    generate_totp_result = generate_totp(secret=TOTP_SECRET_KEY)
    if generate_totp_result[0] != SUCCESS:
        print(f"generate_totp failure - {generate_totp_result[1]}")
        sys.exit()
    else:
        print("generate_totp success")

    # Step 3 - Verify totp and get request key from verify_totp Function.
    request_key = verify_client_id_result[1]
    totp = generate_totp_result[1]
    verify_totp_result = verify_totp(request_key=request_key, totp=totp)
    if verify_totp_result[0] != SUCCESS:
        print(f"verify_totp_result failure - {verify_totp_result[1]}")
        sys.exit()
    else:
        print("verify_totp_result success")
    
    # Step 4 - Verify pin and send back access token
    request_key_2 = verify_totp_result[1]
    verify_pin_result = verify_PIN(request_key=request_key_2, pin=PIN)
    if verify_pin_result[0] != SUCCESS:
        print(f"verify_pin_result failure - {verify_pin_result[1]}")
        sys.exit()
    else:
        print("verify_pin_result success")
    
    # Step 5 - Get auth code for API V3 App from trade access token
    token_result = token(
        client_id=CLIENT_ID, app_id=APP_ID, redirect_uri=REDIRECT_URI, app_type=APP_TYPE,
        access_token=verify_pin_result[1]
    )
    if token_result[0] != SUCCESS:
        print(f"token_result failure - {token_result[1]}")
        sys.exit()
    else:
        print("token_result success")

    # Step 6 - Get API V3 access token from validating auth code
    auth_code = token_result[1]
    validate_authcode_result = validate_authcode(auth_code=auth_code)
    if token_result[0] != SUCCESS:
        print(f"validate_authcode failure - {validate_authcode_result[1]}")
        sys.exit()
    else:
        print("validate_authcode success")
    
    with open('access_token.txt', 'w') as k:
        k.write(validate_authcode_result[1])

    access_token = APP_ID + "-" + APP_TYPE + ":" + validate_authcode_result[1]

    print(f"\naccess_token - {access_token}\n")
    return validate_authcode_result[1]

if __name__ == "__main__":
    main()
