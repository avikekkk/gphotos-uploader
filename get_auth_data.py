#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests"]
# ///
"""
Turn a Google Embedded Setup `oauth_token` into a gpmc `auth_data` string.

This replaces the whole ADB / ReVanced Android dance. It does exactly what the
gotohp GUI does internally: exchange the oauth_token for a master token, then
assemble the credential string that gpmc's Client(auth_data=...) expects.

HOW TO GET THE oauth_token (same as gotohp Option 1 "Embedded setup"):
  1. Open this URL in a browser and sign in with the target account:
       https://accounts.google.com/EmbeddedSetup
  2. Click "I agree". The page may keep loading forever — that's expected.
  3. Open DevTools -> Application/Storage -> Cookies -> accounts.google.com
  4. Copy the VALUE of the `oauth_token` cookie (starts with "oauth2_4/...").

USAGE:
  python get_auth_data.py "PASTE_OAUTH_TOKEN_HERE"

It prints the auth_data line. Put it in config.py as ANDROID_CREDS, or export
it as GP_AUTH_DATA. The token is single-use-ish and the master token it yields
is long-lived, so you only do this once per account.

Requires: requests  (already in requirements.txt)
"""

import sys
import secrets
from urllib.parse import urlencode, parse_qsl

import requests

# ── Constants copied from gotohp's backend/googleauth.go (known-good values) ──
AUTH_ENDPOINT = "https://android.clients.google.com/auth"
GMS_SIG = "38918a453d07199354f8b19af05ec6562ced5788"       # Play Services cert
PHOTOS_PKG = "com.google.android.apps.photos"
PHOTOS_SIG = "24bb24c05e47e0aefa68a58a766179d9b613a600"     # Photos app cert
PHOTOS_SERVICE = ("oauth2:openid "
                  "https://www.googleapis.com/auth/mobileapps.native "
                  "https://www.googleapis.com/auth/photos.native")
GMS_VERSION = "240913000"


def generate_android_id() -> str:
    """16 hex chars = 8 random bytes, same as gotohp."""
    return secrets.token_hex(8)


def normalize_oauth_token(value: str) -> str:
    value = value.strip()
    if value.startswith("oauth_token="):
        value = value[len("oauth_token="):]
    if not (16 <= len(value) <= 8192) or "\n" in value or "\r" in value:
        raise SystemExit("[!] That doesn't look like an oauth_token cookie value.")
    return value


def exchange_token(oauth_token: str, android_id: str) -> tuple[str, str]:
    """oauth_token -> (email, master_token) via Google's android auth endpoint."""
    form = {
        "accountType": "HOSTED_OR_GOOGLE",
        "Email": "oauth-token@example.com",
        "has_permission": "1",
        "add_account": "1",
        "ACCESS_TOKEN": "1",
        "Token": oauth_token,
        "service": "ac2dm",
        "source": "android",
        "androidId": android_id,
        "device_country": "us",
        "operatorCountry": "us",
        "lang": "en",
        "sdk_version": "17",
        "google_play_services_version": GMS_VERSION,
        "client_sig": GMS_SIG,
        "callerSig": GMS_SIG,
        "droidguard_results": "dummy123",
    }
    resp = requests.post(
        AUTH_ENDPOINT,
        data=form,
        headers={
            "Accept-Encoding": "identity",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "GoogleAuth/1.4",
        },
        timeout=30,
        allow_redirects=False,
    )
    values = {}
    for line in resp.text.splitlines():
        line = line.strip()
        if "=" in line:
            k, v = line.split("=", 1)
            values[k] = v

    if values.get("Error"):
        hint = {
            "BadAuthentication": "oauth_token rejected — get a FRESH one and retry.",
            "NeedsBrowser": "Google wants a fresh Embedded Setup sign-in.",
            "MissingDroidguard": "Google rejected the device verification data.",
        }.get(values["Error"], "")
        raise SystemExit(f"[!] Google auth error: {values['Error']}. {hint}")

    master = values.get("Token")
    email = values.get("Email")
    if not master:
        raise SystemExit(f"[!] No master token in response. HTTP {resp.status_code}. "
                         f"Body: {resp.text[:200]}")
    if not email:
        raise SystemExit("[!] No account email in response.")
    return email, master


def build_auth_data(email: str, master_token: str, android_id: str) -> str:
    """Assemble the gpmc-compatible auth_data query string."""
    values = {
        "androidId": android_id,
        "app": PHOTOS_PKG,
        "callerPkg": PHOTOS_PKG,
        "callerSig": PHOTOS_SIG,
        "client_sig": PHOTOS_SIG,
        "device_country": "us",
        "Email": email,
        "google_play_services_version": GMS_VERSION,
        "lang": "en_US",
        "oauth2_foreground": "1",
        "operatorCountry": "us",
        "sdk_version": "33",
        "service": PHOTOS_SERVICE,
        "source": "android",
        "Token": master_token,
    }
    return urlencode(values)


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    oauth_token = normalize_oauth_token(sys.argv[1])
    android_id = generate_android_id()

    print("[*] Exchanging oauth_token for a master token...")
    email, master = exchange_token(oauth_token, android_id)
    print(f"[+] Account: {email}")

    auth_data = build_auth_data(email, master, android_id)

    # Sanity: confirm the fields gpmc reads are all present.
    keys = dict(parse_qsl(auth_data))
    required = ["androidId", "client_sig", "callerSig", "device_country", "Email",
                "google_play_services_version", "lang", "oauth2_foreground",
                "sdk_version", "service", "Token"]
    missing = [k for k in required if k not in keys]
    if missing:
        raise SystemExit(f"[!] Built credential is missing fields: {missing}")

    print("\n[+] SUCCESS — this is your ANDROID_CREDS / GP_AUTH_DATA value:\n")
    print(auth_data)
    print("\n[i] Paste it into config.py as ANDROID_CREDS (keep it secret).")


if __name__ == "__main__":
    main()
