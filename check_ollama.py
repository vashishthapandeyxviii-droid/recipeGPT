"""Diagnostic only: shows WHY the backend cannot get an answer from Ollama.
It uses the same URL and model name as main.py and changes nothing.
Run:  uv run python check_ollama.py"""
import time

import httpx

import main  # only to read OLLAMA_URL and OLLAMA_MODEL, so we test exactly what the app uses

base = main.OLLAMA_URL.replace("/api/chat", "")
print(f"Backend is configured to use: {main.OLLAMA_URL}  model={main.OLLAMA_MODEL}\n")

# 1) Is the Ollama server reachable, and does it list the model?
try:
    r = httpx.get(base + "/api/tags", timeout=5)
    names = [m["name"] for m in r.json().get("models", [])]
    print(f"1) Server reachable (HTTP {r.status_code}). Models installed: {names}")
    print("   Configured model installed:", any(n == main.OLLAMA_MODEL or n.startswith(main.OLLAMA_MODEL) for n in names))
except Exception as e:
    print(f"1) CANNOT REACH OLLAMA -> {type(e).__name__}: {e}")
    print("   Ollama is probably not running. Start the Ollama app, or run 'ollama serve' in another window.")
    raise SystemExit(1)

# 2) Which models are loaded in memory right now?
try:
    r = httpx.get(base + "/api/ps", timeout=5)
    print("2) Loaded in memory:", [m["name"] for m in r.json().get("models", [])] or "nothing yet")
except Exception as e:
    print(f"2) could not read /api/ps -> {type(e).__name__}: {e}")

# 3) The same kind of request the backend sends
payload = {
    "model": main.OLLAMA_MODEL,
    "stream": False,
    "think": False,
    "messages": [{"role": "user", "content": "Say hi in five words."}],
}
print("3) Sending a test question (the first call can be slow while the model loads)...")
start = time.time()
try:
    r = httpx.post(main.OLLAMA_URL, json=payload, timeout=180)
    took = time.time() - start
    if r.status_code != 200:
        print(f"   FAILED: HTTP {r.status_code} after {took:.1f}s. Ollama said: {r.text[:300]}")
    else:
        print(f"   OK in {took:.1f}s. Qwen replied: {r.json()['message']['content']!r}")
        print(f"   (the backend waits up to {main.OLLAMA_TIMEOUT}s)")
except Exception as e:
    print(f"   FAILED after {time.time() - start:.1f}s -> {type(e).__name__}: {e}")
