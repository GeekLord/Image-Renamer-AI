"""Check the Inferway API key and model: list models, then stream one short reply."""
import os
import sys
import json
import time
from typing import Optional, Dict, Any

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import httpx
except ImportError:
    httpx = None

import urllib.request
import urllib.error

BASE_URL = "https://api.inferway.ai/v1"
MODEL_ID = "deepseek/deepseek-v4.1-flash-1017"


def get_api_key() -> str:
    key = os.environ.get("INFERWAY_API_KEY")
    if not key:
        print("ERROR: INFERWAY_API_KEY is not set.", file=sys.stderr)
        print("Set the environment variable or create a .env file.", file=sys.stderr)
        sys.exit(1)
    return key


def fetch_models(api_key: str) -> Dict[str, Any]:
    """Return the raw /models response (3 attempts); raises on final failure."""
    url = f"{BASE_URL}/models"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data
        except urllib.error.HTTPError as exc:
            status = exc.code
            error_code = "UNKNOWN"
            try:
                body = json.loads(exc.read().decode("utf-8"))
                if isinstance(body, dict):
                    error_obj = body.get("error", {})
                    if isinstance(error_obj, dict):
                        error_code = error_obj.get("code") or error_obj.get("message") or "UNKNOWN"
                    else:
                        error_code = str(error_obj)
            except Exception:
                pass
            print(f"[Attempt {attempt + 1}] Request failed: HTTP {status}, Error Code: {error_code}", file=sys.stderr)
            if attempt == 2:
                raise
            time.sleep(1)
        except Exception as exc:
            print(f"[Attempt {attempt + 1}] Request error: {exc}", file=sys.stderr)
            if attempt == 2:
                raise
            time.sleep(1)
    raise RuntimeError("Failed to fetch models after retries.")


def run_smoke_test(api_key: str, model_id: str, context_length: Optional[int]) -> None:
    """Stream a one-line chat reply and print it; exit 1 after 3 failed attempts."""
    url = f"{BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model_id,
        "messages": [
            {"role": "user", "content": "Respond with 'Smoke test passed' and nothing else."}
        ],
        "stream": True
    }
    if context_length:
        payload["max_tokens"] = min(50, context_length)

    print(f"Connecting to {BASE_URL} with model {model_id}...")
    if context_length:
        print(f"Model context length limit: {context_length} tokens")

    for attempt in range(3):
        try:
            req_data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=req_data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=60) as resp:
                print("Streaming response: ", end="", flush=True)
                for line_bytes in resp:
                    line = line_bytes.decode("utf-8").strip()
                    if not line:
                        continue
                    if line.startswith("data: "):
                        content = line[6:]
                        if content == "[DONE]":
                            break
                        try:
                            chunk = json.loads(content)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            text = delta.get("content", "")
                            if text:
                                print(text, end="", flush=True)
                        except json.JSONDecodeError:
                            pass
                print("\nSmoke test completed successfully.")
                return
        except urllib.error.HTTPError as exc:
            status = exc.code
            error_code = "UNKNOWN"
            try:
                body = json.loads(exc.read().decode("utf-8"))
                if isinstance(body, dict):
                    error_obj = body.get("error", {})
                    if isinstance(error_obj, dict):
                        error_code = error_obj.get("code") or error_obj.get("message") or "UNKNOWN"
                    else:
                        error_code = str(error_obj)
            except Exception:
                pass
            print(f"[Attempt {attempt + 1}] Request failed: HTTP {status}, Error Code: {error_code}", file=sys.stderr)
            if attempt == 2:
                sys.exit(1)
            time.sleep(1)
        except Exception as exc:
            print(f"[Attempt {attempt + 1}] Connection error: {exc}", file=sys.stderr)
            if attempt == 2:
                sys.exit(1)
            time.sleep(1)


def main():
    api_key = get_api_key()
    print("Fetching available models...")
    models_data = fetch_models(api_key)
    models = models_data.get("data", [])

    chosen_model = None
    for m in models:
        if m.get("id") == MODEL_ID:
            chosen_model = m
            break

    if not chosen_model:
        print(f"Target model '{MODEL_ID}' was not found in the models list.")
        print("Available models:")
        for m in models:
            print(f"- {m.get('id')}")
        sys.exit(1)

    context_length = chosen_model.get("context_length")
    print(f"Selected model: {MODEL_ID} (context_length: {context_length})")
    run_smoke_test(api_key, MODEL_ID, context_length)


if __name__ == "__main__":
    main()
