"""Optional Locust load test for a LOCAL VR_RAG server only.

Never run this against the production Render URL.
Requires: pip install locust
"""
import os
from locust import HttpUser, between, task

# Safety guard: refuse non-local targets unless explicitly overridden for local Docker/LAN testing.
DEFAULT_HOST = "http://127.0.0.1:8000"


class GlassesUser(HttpUser):
    wait_time = between(0.2, 1.0)
    host = DEFAULT_HOST

    def on_start(self):
        token = os.environ.get("VR_TOKEN", "")
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        if "onrender.com" in (self.host or "").lower():
            raise RuntimeError("Refusing to run load test against a Render production URL.")

    @task(5)
    def search(self):
        self.client.post("/api/v1/search", json={"query": "safety procedure"}, headers=self.headers)

    @task(2)
    def ask_offline_or_ai(self):
        self.client.post("/api/v1/ask", json={"question": "what is the trading window?"}, headers=self.headers)

    @task(2)
    def health(self):
        self.client.get("/api/v1/health")
