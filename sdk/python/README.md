# `rag-vr-sdk` (Python)

Zero-dependency typed Python client for the RAG-VR-AI `/api/v1` partner API (uses stdlib `urllib.request`, with an injectable `transport` hook for testing).

## Quickstart

```python
from rag_vr_sdk import RagVrClient, RagVrApiError

client = RagVrClient(
    base_url="http://localhost:8000",
    token="vrk_your_scoped_key",  # vrk_... -> Authorization: Bearer, otherwise X-VR-Token
)

health = client.health()
ans = client.ask("What is the torque spec?", style="glance")

for ev in client.ask_stream("Summarize safety steps", style="spoken"):
    if ev["event"] == "delta":
        print(ev["data"]["text"], end="", flush=True)
```
