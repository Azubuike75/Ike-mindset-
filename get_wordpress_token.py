#!/usr/bin/env python3
"""
Run this ONCE, on your own computer, to get a WordPress.com access token.
The token it prints out never expires (unless you revoke it) — save it as
the WP_ACCESS_TOKEN environment variable in Render, and you never need to
run this again.

SETUP (5 minutes, one time):
1. Go to https://developer.wordpress.com/apps/ and log in with your
   WordPress.com account.
2. Click "Create New Application".
   - Type: "Web"
   - Redirect URI: http://localhost:8899/callback
   - (name/description can be anything, e.g. "Novel Writer")
3. After creating it, copy the "Client ID" and "Client Secret" it shows you.
4. Run this script:
       pip install requests --break-system-packages
       python get_wordpress_token.py
   It'll ask for your Client ID and Secret, open a browser tab for you to
   log in and approve access, then print your access token.
5. Also note your site's address (e.g. "yourname.wordpress.com") — you'll
   need that too, as the WP_SITE environment variable.
"""

import http.server
import urllib.parse
import webbrowser
import requests

REDIRECT_URI = "http://localhost:8899/callback"
AUTH_URL = "https://public-api.wordpress.com/oauth2/authorize"
TOKEN_URL = "https://public-api.wordpress.com/oauth2/token"

received_code = {}


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" in params:
            received_code["code"] = params["code"][0]
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Success! You can close this tab and return to your terminal.</h1>")
        else:
            self.send_response(400)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # silence default logging


def main():
    client_id = input("Paste your WordPress.com Client ID: ").strip()
    client_secret = input("Paste your WordPress.com Client Secret: ").strip()

    auth_params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "global",
    }
    auth_url = f"{AUTH_URL}?{urllib.parse.urlencode(auth_params)}"
    print(f"\nOpening your browser to log in and approve access...\n"
          f"If it doesn't open automatically, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)

    server = http.server.HTTPServer(("localhost", 8899), CallbackHandler)
    print("Waiting for you to approve access in the browser...")
    while "code" not in received_code:
        server.handle_request()

    code = received_code["code"]
    print("Got authorization code, exchanging for access token...")

    resp = requests.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    })

    if resp.status_code != 200:
        print(f"Something went wrong: {resp.status_code} {resp.text}")
        return

    token_data = resp.json()
    access_token = token_data.get("access_token")
    blog_id = token_data.get("blog_id")
    blog_url = token_data.get("blog_url")

    print("\n" + "=" * 60)
    print("SUCCESS — save these as environment variables in Render:")
    print("=" * 60)
    print(f"WP_ACCESS_TOKEN = {access_token}")
    print(f"WP_SITE         = {blog_url.replace('https://', '').replace('http://', '') if blog_url else '(use your site address, e.g. yourname.wordpress.com)'}")
    print("=" * 60)


if __name__ == "__main__":
    main()
