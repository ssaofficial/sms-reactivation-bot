"""Debug GHL bridge responses to diagnose conversation ID and SMS issues."""
import json
import requests
import logging
logging.basicConfig(level=logging.DEBUG)

from config import GHL_BRIDGE_URL, GHL_BEARER_TOKEN, GHL_LOCATION_ID

CONTACT_ID = "hIzDqdWWC458rzNA3LKg"

def raw_call(tool, params):
    payload = {
        "locationId": GHL_LOCATION_ID,
        "tool": tool,
        "parameters": params
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GHL_BEARER_TOKEN}"
    }
    resp = requests.post(GHL_BRIDGE_URL, json=payload, headers=headers, timeout=60)
    print(f"\n=== {tool} ===")
    print(f"Status: {resp.status_code}")
    try:
        data = resp.json()
        print(f"Raw response: {json.dumps(data, indent=2)[:2000]}")
        # Try to extract text content
        if isinstance(data, list) and data:
            content = data[0].get("result", {}).get("content", [])
            if content:
                for item in content:
                    if item.get("type") == "text":
                        try:
                            parsed = json.loads(item["text"])
                            print(f"Parsed content: {json.dumps(parsed, indent=2)[:2000]}")
                        except:
                            print(f"Text content: {item['text'][:2000]}")
    except Exception as e:
        print(f"Error parsing: {e}")
        print(f"Raw text: {resp.text[:2000]}")
    return resp

# Test 1: get_conversation
raw_call("get_conversation", {"contactId": CONTACT_ID})

# Test 2: search_conversations  
raw_call("search_conversations", {"contactId": CONTACT_ID})

# Test 3: Try send_message directly without conversation ID
raw_call("send_message", {
    "type": "SMS",
    "contactId": CONTACT_ID,
    "message": "debug test - ignore"
})
