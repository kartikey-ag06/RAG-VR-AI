export type AnswerStyle = "full" | "glance" | "spoken";

export interface HealthResponse {
  api: string;
  ok: boolean;
  ai: boolean;
  provider: string;
  photo: boolean;
  documents: number;
  index_version: number | string;
  styles: AnswerStyle[];
  doors: {
    mcp: string;
    hud: string;
    webxr: string;
    [key: string]: string;
  };
}

export interface DocumentSummary {
  doc_uid: string;
  title: string;
  pages: number;
  type: string;
  machine_ids: string[];
}

export interface DocumentsResponse {
  documents: DocumentSummary[];
  index_version: number | string;
}

export interface SearchRequest {
  query: string;
  k?: number;
  machine_id?: string;
  machine?: string;
}

export interface Passage {
  doc_uid: string;
  title: string;
  page: number;
  text: string;
  score: number;
  image_url: string;
  n?: number;
  i?: number;
  semantic?: number | null;
  keyword?: number | null;
}

export interface SearchResponse {
  query: string;
  mode: string | null;
  engine: string | null;
  confidence: number | null;
  low_confidence: boolean;
  machine: string | null;
  machine_matched: boolean | null;
  index_version: number | string | null;
  passages: Passage[];
}

export interface PhotoHint {
  object?: string;
  details?: string;
  text_seen?: string;
  keywords?: string;
}

export interface PassageRef {
  doc_uid: string;
  page: number;
  i?: number;
}

export interface AskRequest {
  question: string;
  query?: string;
  machine_id?: string;
  style?: AnswerStyle;
  lang?: string;
  photo?: PhotoHint;
  passages?: PassageRef[];
}

export interface Citation {
  n: number;
  doc_uid: string;
  title: string;
  page: number;
  pages: number | null;
  image_url: string;
  image_url_small: string;
}

export interface AskResponse {
  answer: string;
  mode: "pdf" | "general" | "offline" | "gate" | string;
  citations: Citation[];
  confidence: number | null;
  search_mode: string | null;
  cited?: number[];
  note?: string;
}

export interface StreamMetaData {
  search_mode: string | null;
  confidence: number | null;
  candidates: Citation[];
}

export interface StreamDeltaData {
  text: string;
  answer_so_far: string;
}

export type StreamEvent =
  | { event: "meta"; data: StreamMetaData }
  | { event: "delta"; data: StreamDeltaData }
  | { event: "done"; data: AskResponse };

export interface ScanRequest {
  image: string;
  question?: string;
  query?: string;
  machine_id?: string;
  style?: AnswerStyle;
  lang?: string;
  hint?: string;
  session_id?: string;
  gate?: boolean;
}

export interface GateMetrics {
  blur_score: number;
  dhash: string;
  hamming_distance: number | null;
  is_blurry: boolean;
  is_duplicate: boolean;
}

export interface EntityState {
  active_entity: string;
  turn_count: number;
  recent_turns: number;
}

export interface ScanPhotoSummary {
  object?: string;
  details?: string;
  text_seen?: string;
}

export interface ScanResponse {
  status: "ok" | "skipped_blur" | "skipped_duplicate" | "reused_scene" | string;
  cached: boolean;
  answer: string;
  mode: string;
  citations: Citation[];
  confidence: number | null;
  search_mode?: string | null;
  note?: string;
  photo: ScanPhotoSummary;
  session_id: string;
  gate_metrics: GateMetrics | null;
  entity_state: EntityState;
}

export interface AgentRunRequest {
  question: string;
  max_steps?: number;
}

export interface AgentStep {
  step: number;
  tool: string;
  read_only: boolean;
  ok?: boolean;
  approved?: boolean;
}

export interface AgentRunResponse {
  status: "completed" | "awaiting_approval" | "step_limit_reached" | string;
  answer?: string;
  approval_id?: string;
  tool?: string;
  args?: Record<string, unknown>;
  steps: AgentStep[];
}

export interface ApprovalRecord {
  approval_id: string;
  tenant_id: string;
  tool_name: string;
  module_id: string | null;
  required_scope: string;
  args: Record<string, unknown>;
  status: "pending" | "approved" | "rejected" | string;
  created_at: number;
  expires_at: number;
}

export interface AgentRejectResponse {
  status: "rejected" | string;
  approval_id: string;
  tool: string;
}
