#!/usr/bin/env python3
"""Ask the live development skill for departures at 8 am and check the answer.

    python3 scripts/smoke_test.py write request.json
    ask smapi invoke-skill-end-point ... --skill-request-body file:request.json > out.json
    python3 scripts/smoke_test.py check out.json

The request carries a v1 account-linking token for Warszawa Zachodnia, the
station a busy hour always has trains at.
"""
import base64
import json
import sys
import uuid
from datetime import datetime, timezone

SKILL_ID = "amzn1.ask.skill.fc4951e6-10b0-4b55-9b70-c52f081a435a"
STATION = "33506"  # Warszawa Zachodnia
TOKEN = "v1." + base64.urlsafe_b64encode(json.dumps([STATION]).encode()).decode().rstrip("=")


def write(path):
    user = {"userId": "amzn1.ask.account.github-smoke", "accessToken": TOKEN}
    body = {
        "version": "1.0",
        "session": {
            "new": True,
            "sessionId": "amzn1.echo-api.session." + str(uuid.uuid4()),
            "application": {"applicationId": SKILL_ID},
            "user": user,
            "attributes": {},
        },
        "context": {
            "System": {
                "application": {"applicationId": SKILL_ID},
                "user": user,
                "device": {"deviceId": "amzn1.ask.device.TEST", "supportedInterfaces": {}},
                "apiEndpoint": "https://api.eu.amazonalexa.com",
            }
        },
        "request": {
            "type": "IntentRequest",
            "requestId": "amzn1.echo-api.request." + str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "locale": "en-GB",
            "dialogState": "STARTED",
            "intent": {
                "name": "DeparturesIntent",
                "confirmationStatus": "NONE",
                "slots": {
                    "time": {"name": "time", "confirmationStatus": "NONE", "value": "08:00"},
                    "date": {"name": "date", "confirmationStatus": "NONE"},
                },
            },
        },
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(body, handle)


def check(path):
    raw = open(path, encoding="utf-8").read()
    try:
        data = json.loads(raw)
    except ValueError:
        sys.exit("smoke test: the CLI did not return JSON:\n" + raw[:2000])
    result = data.get("result") or {}
    if data.get("status") != "SUCCESSFUL" or result.get("error"):
        sys.exit(f"smoke test: invocation failed: {data.get('status')} {result.get('error')}")
    response = result["skillExecutionInfo"]["invocationResponse"]["body"]["response"]
    speech = (response.get("outputSpeech") or {}).get("ssml", "")
    print("speech:", speech[:600])
    expected = ["Departures from eight am", "Zachodnia", "Towards"]
    missing = [text for text in expected if text not in speech]
    if missing:
        sys.exit(f"smoke test: the answer is missing {missing}")
    print("smoke test passed")


if __name__ == "__main__":
    {"write": write, "check": check}[sys.argv[1]](sys.argv[2])
