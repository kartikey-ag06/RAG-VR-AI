"""Typed request/response definitions for the RAG-VR-AI /api/v1 partner API."""

from typing import Any, Dict, List, Literal, Optional, TypedDict

AnswerStyle = Literal["full", "glance", "spoken"]


class HealthDoors(TypedDict, total=False):
    mcp: str
    hud: str
    webxr: str


class HealthResponse(TypedDict):
    api: str
    ok: bool
    ai: bool
    provider: str
    photo: bool
    documents: int
    index_version: Any
    styles: List[str]
    doors: HealthDoors


class DocumentSummary(TypedDict):
    doc_uid: str
    title: str
    pages: int
    type: str
    machine_ids: List[str]


class DocumentsResponse(TypedDict):
    documents: List[DocumentSummary]
    index_version: Any


class Passage(TypedDict, total=False):
    doc_uid: str
    title: str
    page: int
    text: str
    score: float
    image_url: str
    n: int
    i: int
    semantic: Optional[float]
    keyword: Optional[float]


class SearchResponse(TypedDict, total=False):
    query: str
    mode: Optional[str]
    engine: Optional[str]
    confidence: Optional[float]
    low_confidence: bool
    machine: Optional[str]
    machine_matched: Optional[bool]
    index_version: Any
    passages: List[Passage]


class PhotoHint(TypedDict, total=False):
    object: str
    details: str
    text_seen: str
    keywords: str


class PassageRef(TypedDict, total=False):
    doc_uid: str
    page: int
    i: int


class Citation(TypedDict):
    n: int
    doc_uid: str
    title: str
    page: int
    pages: Optional[int]
    image_url: str
    image_url_small: str


class AskResponse(TypedDict, total=False):
    answer: str
    mode: str
    citations: List[Citation]
    confidence: Optional[float]
    search_mode: Optional[str]
    cited: List[int]
    note: str


class StreamEvent(TypedDict):
    event: Literal["meta", "delta", "done"]
    data: Dict[str, Any]


class GateMetrics(TypedDict):
    blur_score: float
    dhash: str
    hamming_distance: Optional[int]
    is_blurry: bool
    is_duplicate: bool


class EntityState(TypedDict):
    active_entity: str
    turn_count: int
    recent_turns: int


class ScanPhotoSummary(TypedDict, total=False):
    object: str
    details: str
    text_seen: str


class ScanResponse(TypedDict, total=False):
    status: str
    cached: bool
    answer: str
    mode: str
    citations: List[Citation]
    confidence: Optional[float]
    search_mode: Optional[str]
    note: str
    photo: ScanPhotoSummary
    session_id: str
    gate_metrics: Optional[GateMetrics]
    entity_state: EntityState


class AgentStep(TypedDict, total=False):
    step: int
    tool: str
    read_only: bool
    ok: bool
    approved: bool


class AgentRunResponse(TypedDict, total=False):
    status: str
    answer: str
    approval_id: str
    tool: str
    args: Dict[str, Any]
    steps: List[AgentStep]


class ApprovalRecord(TypedDict):
    approval_id: str
    tenant_id: str
    tool_name: str
    module_id: Optional[str]
    required_scope: str
    args: Dict[str, Any]
    status: str
    created_at: float
    expires_at: float


class AgentRejectResponse(TypedDict):
    status: str
    approval_id: str
    tool: str
