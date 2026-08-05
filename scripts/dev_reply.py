"""Post a *signed* inbound WhatsApp reply to a running ops-core, the way a BSP would.
Handy for demos and for documenting the webhook contract.

    python -m scripts.dev_reply loom_5 1                  # reply "1" for loom_5
    python -m scripts.dev_reply loom_5 1 +919000000005    # from a specific number

Signs the body with WHATSAPP_WEBHOOK_SECRET (HMAC-SHA256) and sends X-Signature.
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
    body = json.dumps({"from": sender, "text": digit, "asset_ref": asset_ref}).encode()

    secret = os.environ.get("WHATSAPP_WEBHOOK_SECRET", "")
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Signature"] = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    resp = requests.post(f"{BASE}/webhook/whatsapp", data=body, headers=headers, timeout=10)
    print(resp.status_code, resp.text)


if __name__ == "__main__":
    main()
