"""
One-time Gmail authorization for Amy.

    python authorize.py --client-secret client_secret.json

Opens a browser to sign in as the intake account (valeconsultingaz@gmail.com)
and writes token.json, which main.py uses to run unattended. Keep both files
out of source control.
"""
import argparse

from google_auth_oauthlib.flow import InstalledAppFlow

from gmail_client import SCOPES


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client-secret", default="client_secret.json",
                        help="OAuth client file downloaded from Google Cloud Console (Desktop App type)")
    parser.add_argument("--token", default="token.json", help="Where to write the token")
    args = parser.parse_args()

    flow = InstalledAppFlow.from_client_secrets_file(args.client_secret, SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    with open(args.token, "w") as f:
        f.write(creds.to_json())
    print(f"Wrote {args.token}")


if __name__ == "__main__":
    main()
