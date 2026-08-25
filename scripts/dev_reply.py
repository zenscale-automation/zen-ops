"""Post a *signed* inbound WhatsApp reply to a running ops-core, the way a BSP would.
Handy for demos and for documenting the webhook contract.

    python -m scripts.dev_reply loom_5 1                  # reply "1" for loom_5
    python -m scripts.dev_reply loom_5 1 +919000000005    # from a specific number

Signs the body the way the webhook actually verifies it: X-Hub-Signature-256 with WHATSAPP_APP_SECRET. Unset means unsigned (dev).
"""

import hashlib
import hmac
import json
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

BASE = os.environ.get("OPS_BASE_URL", "http://127.0.0.1:8000")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    asset_ref, digit = sys.argv[1], sys.argv[2]
    sender = sys.argv[3] if len(sys.argv) > 3 else "+919000000005"
    # The PickyAssist inbound shape — the one dialect the webhook actually parses now.
    # The old flat {"from": ...} form is dead: it was the invented BSP-placeholder shape,
    # and posting it exercised nothing but the ignore path.
    body = json.dumps({"number": sender.lstrip("+"), "message-in": digit,
                       "message_in_raw": digit, "direction": 0,
                       "unique-id": "dev-" + digit}).encode()

    secret = os.environ.get("WHATSAPP_APP_SECRET", "")
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    resp = requests.post(f"{BASE}/webhook/whatsapp", data=body, headers=headers, timeout=10)
    print(resp.status_code, resp.text)


if __name__ == "__main__":
    main()
