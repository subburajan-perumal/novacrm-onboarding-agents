"""One-time Gmail OAuth consent: prints the consent URL, waits for the redirect, saves secrets/token.json."""
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

ROOT = Path(__file__).resolve().parents[1]
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
flow = InstalledAppFlow.from_client_secrets_file(str(ROOT / "secrets" / "credentials.json"), SCOPES)
creds = flow.run_local_server(port=8765, open_browser=False,
                              authorization_prompt_message="AUTH_URL {url}", success_message="Gmail connected. You can close this tab.")
(ROOT / "secrets" / "token.json").write_text(creds.to_json())
print("TOKEN_SAVED", flush=True)
