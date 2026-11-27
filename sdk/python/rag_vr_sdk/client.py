"""Zero-dependency HTTP client for RAG-VR-AI /api/v1 using stdlib urllib."""

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple

from .types import (
    AgentRejectResponse,
    AgentRunResponse,
    AnswerStyle,
    ApprovalRecord,
    AskResponse,
    DocumentsResponse,
    HealthResponse,
    PassageRef,
    PhotoHint,
    ScanResponse,
    SearchResponse,
    StreamEvent,
)

TransportResponse = Tuple[int, Mapping[str, str], bytes]
TransportFn = Callable[[str, str, Dict[str, str], Optional[bytes], float], TransportResponse]


class RagVrApiError(Exception):
    """Raised when the /api/v1 server returns a non-2xx HTTP status."""

    def __init__(
        self,
        status: int,
        message: str,
        retry_after: Optional[int] = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after
        self.body = body


def _urllib_transport(
    method: str,
    url: str,
    headers: Dict[str, str],
    body: Optional[bytes],
    timeout: float,
) -> TransportResponse:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as err:
        raw = err.read() if err.fp is not None else b""
        return int(err.code), dict(err.headers.items()), raw


def _header_ci(headers: Mapping[str, str], name: str) -> Optional[str]:
    target = name.lower()
    for k, v in headers.items():
        if k.lower() == target:
            return v
    return None


class RagVrClient:
    """Typed client for the RAG-VR-AI /api/v1 partner API."""

    def __init__(
        self,
        base_url: str,
        token: str = "",
        timeout: float = 30.0,
        transport: Optional[TransportFn] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = (token or "").strip()
        self.timeout = float(timeout)
        self._transport = transport or _urllib_transport

    def _auth_headers(self) -> Dict[str, str]:
        if not self.token:
            return {}
        if self.token.startswith("vrk_"):
            return {"Authorization": f"Bearer {self.token}"}
        return {"X-VR-Token": self.token}

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> TransportResponse:
        headers: Dict[str, str] = self._auth_headers()
        if extra_headers:
            headers.update(extra_headers)
        body: Optional[bytes] = None
        if payload is not None:
            headers.setdefault("Content-Type", "application/json")
            body = json.dumps(payload).encode("utf-8")

        url = f"{self.base_url}{path}"
        status, resp_headers, raw = self._transport(method, url, headers, body, self.timeout)
        if status < 200 or status >= 300:
            retry_raw = _header_ci(resp_headers, "retry-after")
            retry_after: Optional[int] = None
            if retry_raw is not None:
                try:
                    retry_after = int(retry_raw)
                except ValueError:
                    retry_after = None

            parsed_body: Any = None
            message = f"HTTP {status}"
            if raw:
                text = raw.decode("utf-8", errors="replace")
                try:
                    parsed_body = json.loads(text)
                    if isinstance(parsed_body, dict) and "error" in parsed_body:
                        message = str(parsed_body["error"])
                except ValueError:
                    parsed_body = text
                    message = text
            raise RagVrApiError(status=status, message=message, retry_after=retry_after, body=parsed_body)
        return status, resp_headers, raw

    def _json(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        _, _, raw = self._request(method, path, payload=payload)
        return json.loads(raw.decode("utf-8"))

    def health(self) -> HealthResponse:
        return self._json("GET", "/api/v1/health")

    def list_documents(self) -> DocumentsResponse:
        return self._json("GET", "/api/v1/documents")

    def search(
        self,
        query: str,
        *,
        k: Optional[int] = None,
        machine_id: Optional[str] = None,
        machine: Optional[str] = None,
    ) -> SearchResponse:
        payload: Dict[str, Any] = {"query": query}
        if k is not None:
            payload["k"] = k
        if machine_id is not None:
            payload["machine_id"] = machine_id
        if machine is not None:
            payload["machine"] = machine
        return self._json("POST", "/api/v1/search", payload)

    def ask(
        self,
        question: str,
        *,
        machine_id: Optional[str] = None,
        style: Optional[AnswerStyle] = None,
        lang: Optional[str] = None,
        photo: Optional[PhotoHint] = None,
        passages: Optional[List[PassageRef]] = None,
    ) -> AskResponse:
        payload: Dict[str, Any] = {"question": question}
        if machine_id is not None:
            payload["machine_id"] = machine_id
        if style is not None:
            payload["style"] = style
        if lang is not None:
            payload["lang"] = lang
        if photo is not None:
            payload["photo"] = photo
        if passages is not None:
            payload["passages"] = passages
        return self._json("POST", "/api/v1/ask", payload)

    def ask_stream(
        self,
        question: str,
        *,
        machine_id: Optional[str] = None,
        style: Optional[AnswerStyle] = None,
        lang: Optional[str] = None,
        photo: Optional[PhotoHint] = None,
        passages: Optional[List[PassageRef]] = None,
    ) -> Iterator[StreamEvent]:
        payload: Dict[str, Any] = {"question": question}
        if machine_id is not None:
            payload["machine_id"] = machine_id
        if style is not None:
            payload["style"] = style
        if lang is not None:
            payload["lang"] = lang
        if photo is not None:
            payload["photo"] = photo
        if passages is not None:
            payload["passages"] = passages

        _, _, raw = self._request(
            "POST",
            "/api/v1/ask/stream",
            payload=payload,
            extra_headers={"Accept": "text/event-stream"},
        )
        text = raw.decode("utf-8", errors="replace")
        for block in text.replace("\r\n", "\n").split("\n\n"):
            if not block.strip():
                continue
            event_name = ""
            data_lines: List[str] = []
            for line in block.split("\n"):
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
            if event_name in ("meta", "delta", "done") and data_lines:
                yield {"event": event_name, "data": json.loads("\n".join(data_lines))}  # type: ignore[typeddict-item]

    def scan(
        self,
        image: str,
        *,
        question: Optional[str] = None,
        machine_id: Optional[str] = None,
        style: Optional[AnswerStyle] = None,
        lang: Optional[str] = None,
        hint: Optional[str] = None,
        session_id: Optional[str] = None,
        gate: Optional[bool] = None,
    ) -> ScanResponse:
        payload: Dict[str, Any] = {"image": image}
        if question is not None:
            payload["question"] = question
        if machine_id is not None:
            payload["machine_id"] = machine_id
        if style is not None:
            payload["style"] = style
        if lang is not None:
            payload["lang"] = lang
        if hint is not None:
            payload["hint"] = hint
        if session_id is not None:
            payload["session_id"] = session_id
        if gate is not None:
            payload["gate"] = gate
        return self._json("POST", "/api/v1/scan", payload)

    def fetch_page_image(self, doc_uid: str, page: int, width: Optional[int] = None) -> bytes:
        qs = f"?w={int(width)}" if width is not None else ""
        uid_q = urllib.parse.quote(doc_uid, safe="")
        _, _, raw = self._request("GET", f"/api/v1/documents/{uid_q}/pages/{int(page)}.jpg{qs}")
        return raw

    def agent_run(self, question: str, *, max_steps: Optional[int] = None) -> AgentRunResponse:
        payload: Dict[str, Any] = {"question": question}
        if max_steps is not None:
            payload["max_steps"] = max_steps
        return self._json("POST", "/api/v1/agent/run", payload)

    def get_approval(self, approval_id: str) -> ApprovalRecord:
        aid = urllib.parse.quote(approval_id, safe="")
        return self._json("GET", f"/api/v1/agent/approvals/{aid}")

    def approve_agent_action(self, approval_id: str) -> AgentRunResponse:
        aid = urllib.parse.quote(approval_id, safe="")
        return self._json("POST", f"/api/v1/agent/approvals/{aid}/approve")

    def reject_agent_action(self, approval_id: str) -> AgentRejectResponse:
        aid = urllib.parse.quote(approval_id, safe="")
        return self._json("POST", f"/api/v1/agent/approvals/{aid}/reject")
