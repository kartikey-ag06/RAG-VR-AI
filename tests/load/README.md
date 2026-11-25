# Local Load Testing (Optional)

This directory contains an optional [Locust](https://locust.io/) script (`locustfile.py`) for stress-testing a **local** VR_RAG backend instance. It is not executed by `pytest`.

> **Warning:** Never run load tests against the production Render URL.

## How to run locally

1. Install Locust (not in `requirements.txt`):
   ```bash
   pip install locust
   ```
2. Start the backend locally:
   ```bash
   cd backend
   uvicorn app:app --host 127.0.0.1 --port 8000
   ```
3. Run Locust headless (e.g. 20 users, 30s):
   ```bash
   locust -f tests/load/locustfile.py --headless -u 20 -r 5 -t 30s --host http://127.0.0.1:8000
   ```
   (Set `VR_TOKEN=<api_key>` in your environment if `ADMIN_TOKEN` is enabled.)
