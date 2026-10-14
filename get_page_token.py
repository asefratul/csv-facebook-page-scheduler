#!/usr/bin/env python3
"""Turn a short-lived user token from Graph API Explorer into a non-expiring Page access token.

Usage:
    export FB_APP_ID=123456789
    export FB_APP_SECRET=...            # or leave unset and you will be prompted (input hidden)
    python get_page_token.py SHORT_LIVED_USER_TOKEN --page-id PAGE_ID

What it does:
    1. Exchanges the short-lived user token for a long-lived user token.
    2. Uses that to fetch the Page access token (a Page token made this way does not expire).
    3. Verifies the result with Meta's debug_token endpoint and prints PASS/FAIL checks.

Messages go to stderr; the Page token alone is printed to stdout, so you can capture it:
    TOKEN=$(python get_page_token.py "$SHORT" --page-id 123)

Without --page-id it lists the Pages the token can manage, so you can pick the ID.
Treat the output like a password: store it only as the FB_ACCESS_TOKEN secret, never commit it.
Environment: FB_GRAPH_VERSION (default v25.0).
"""
import argparse
import getpass
import os
import sys

import requests

GRAPH_URL = f"https://graph.facebook.com/{os.getenv('FB_GRAPH_VERSION') or 'v25.0'}"
REQUIRED_SCOPES = {"pages_manage_posts", "pages_manage_engagement"}
TIMEOUT = 30


def say(message=""):
    print(message, file=sys.stderr)


def die(message):
    say(f"ERROR: {message}")
    sys.exit(1)


def graph_get(path, params):
    try:
        response = requests.get(f"{GRAPH_URL}/{path}", params=params, timeout=TIMEOUT)
        body = response.json()
    except requests.RequestException as exc:
        die(f"network problem ({type(exc).__name__}). Check your connection and try again.")
    except ValueError:
        die(f"Facebook returned a non-JSON response (HTTP {response.status_code}).")
    if not isinstance(body, dict):
        die(f"unexpected response: {str(body)[:200]}")
    if "error" in body:
        error = body["error"] if isinstance(body["error"], dict) else {}
        die(f"{error.get('message', 'unknown error')} (code {error.get('code')}, subcode {error.get('error_subcode')})")
    return body


def exchange_for_long_lived(app_id, app_secret, short_token):
    body = graph_get("oauth/access_token", {
        "grant_type": "fb_exchange_token",
        "client_id": app_id,
        "client_secret": app_secret,
        "fb_exchange_token": short_token,
    })
    token = body.get("access_token")
    if not token:
        die("Facebook did not return a long-lived token. Is the short-lived token expired (they last about 1 hour) "
            "or generated for a different app than FB_APP_ID?")
    return token


def list_pages(user_token):
    return graph_get("me/accounts", {"access_token": user_token, "fields": "id,name", "limit": 100}).get("data", [])


def fetch_page_token(user_token, page_id):
    body = graph_get(page_id, {"fields": "name,access_token", "access_token": user_token})
    if not body.get("access_token"):
        die(f"no Page token returned for {page_id}. You must be an admin of the Page, and the Page must be ticked "
            "when you granted permissions in Graph API Explorer.")
    return body["access_token"], body.get("name", page_id)


def inspect_token(app_id, app_secret, token):
    return graph_get("debug_token", {"input_token": token, "access_token": f"{app_id}|{app_secret}"}).get("data", {})


def check_token(info, page_id):
    """Return a list of (ok, message) checks for the Page token's debug_token data."""
    scopes = set(info.get("scopes") or [])
    expires = info.get("expires_at")
    checks = [
        (info.get("is_valid") is True, "Token is valid"),
        (info.get("type") == "PAGE", f"Type is PAGE (got {info.get('type')})"),
        (expires == 0, "Expires: never (expires_at = 0)" if expires == 0 else f"Expires: expires_at = {expires} (not 0)"),
    ]
    missing = sorted(REQUIRED_SCOPES - scopes)
    checks.append((not missing, "Has pages_manage_posts and pages_manage_engagement" if not missing
                   else f"Missing permission(s): {', '.join(missing)}"))
    if page_id and info.get("profile_id") and str(info["profile_id"]) != str(page_id):
        checks.append((False, f"Token belongs to Page {info['profile_id']}, not {page_id}"))
    return checks


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create and verify a non-expiring Facebook Page access token.")
    parser.add_argument("short_lived_token", help="User access token copied from Graph API Explorer")
    parser.add_argument("--page-id", help="Numeric Page ID. Omit to list your Pages.")
    args = parser.parse_args(argv)

    app_id = os.getenv("FB_APP_ID") or input("App ID: ").strip()
    app_secret = os.getenv("FB_APP_SECRET") or getpass.getpass("App Secret (hidden): ").strip()
    if not (app_id and app_secret):
        die("App ID and App Secret are required (App settings > Basic).")

    say("1/3 Exchanging for a long-lived user token...")
    user_token = exchange_for_long_lived(app_id, app_secret, args.short_lived_token.strip())

    if not args.page_id:
        pages = list_pages(user_token)
        if not pages:
            die("no Pages were returned. Re-run with --page-id PAGE_ID, and make sure the Page was ticked when you "
                "granted permissions.")
        say("Pages you can manage:")
        for page in pages:
            say(f"  {page['id']}  {page.get('name', '')}")
        say("\nRe-run with --page-id <one of the IDs above>.")
        return 2

    say("2/3 Fetching the Page token...")
    page_token, page_name = fetch_page_token(user_token, args.page_id)

    say("3/3 Verifying...")
    checks = check_token(inspect_token(app_id, app_secret, page_token), args.page_id)
    for ok, message in checks:
        say(f"  [{'PASS' if ok else 'FAIL'}] {message}")
    all_ok = all(ok for ok, _ in checks)
    say(f"\nPage: {page_name} ({args.page_id})")
    say("All checks passed. Save the token below as the FB_ACCESS_TOKEN secret." if all_ok
        else "Some checks FAILED. Do not use this token until they pass (see the README troubleshooting).")
    print(page_token)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
