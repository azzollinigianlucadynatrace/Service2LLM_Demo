import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:5001/chat")

prompts = [
    "What is OpenTelemetry in one sentence?",
    "Explain observability in 20 words.",
    "What are traces and spans?",
]

SLEEP_BETWEEN = 1

print(f"Sending {len(prompts)} prompts to {GATEWAY_URL} (1 round)...\n")

for i, p in enumerate(prompts, 1):
    r = requests.post(GATEWAY_URL, json={"prompt": p}, timeout=30)

    if "application/json" in (r.headers.get("content-type") or ""):
        data = r.json()
        completion = data.get("completion", "")
    else:
        completion = r.text

    print(f"[{i}/{len(prompts)}] {r.status_code}: {completion[:120]}")
    time.sleep(SLEEP_BETWEEN)

print("\nDone. (3 prompts sent, 1 round)")
