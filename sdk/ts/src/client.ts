import type {
  AgentRejectResponse,
  AgentRunRequest,
  AgentRunResponse,
  ApprovalRecord,
  AskRequest,
  AskResponse,
  DocumentsResponse,
  HealthResponse,
  ScanRequest,
  ScanResponse,
  SearchRequest,
  SearchResponse,
  StreamEvent,
} from "./types.js";

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export interface RagVrClientOptions {
  baseUrl: string;
  token?: string;
  fetch?: FetchLike;
}

export class RagVrApiError extends Error {
  readonly status: number;
  readonly retryAfter: number | null;
  readonly body: unknown;

  constructor(status: number, message: string, retryAfter: number | null = null, body: unknown = null) {
    super(message);
    this.name = "RagVrApiError";
    this.status = status;
    this.retryAfter = retryAfter;
    this.body = body;
  }
}

export class RagVrClient {
  private readonly baseUrl: string;
  private readonly token: string;
  private readonly fetchFn: FetchLike;

  constructor(options: RagVrClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.token = (options.token ?? "").trim();
    const fn = options.fetch ?? globalThis.fetch;
    if (!fn) {
      throw new Error("No fetch implementation available; pass options.fetch.");
    }
    this.fetchFn = fn;
  }

  private authHeaders(): Record<string, string> {
    if (!this.token) {
      return {};
    }
    if (this.token.startsWith("vrk_")) {
      return { Authorization: `Bearer ${this.token}` };
    }
    return { "X-VR-Token": this.token };
  }

  private async request(path: string, init: RequestInit = {}): Promise<Response> {
    const headers: Record<string, string> = {
      ...this.authHeaders(),
      ...((init.headers as Record<string, string> | undefined) ?? {}),
    };
    const res = await this.fetchFn(`${this.baseUrl}${path}`, { ...init, headers });
    if (!res.ok) {
      const retryHeader = res.headers.get("retry-after");
      const retryAfter = retryHeader ? Number.parseInt(retryHeader, 10) : null;
      let body: unknown = null;
      let message = `HTTP ${res.status}`;
      try {
        const text = await res.text();
        if (text) {
          try {
            body = JSON.parse(text);
            if (typeof body === "object" && body !== null && "error" in body) {
              message = String((body as { error: unknown }).error);
            }
          } catch {
            body = text;
            message = text;
          }
        }
      } catch {
        // Ignore body read error
      }
      throw new RagVrApiError(res.status, message, Number.isFinite(retryAfter) ? retryAfter : null, body);
    }
    return res;
  }

  private async json<T>(path: string, method = "GET", payload?: unknown): Promise<T> {
    const init: RequestInit = { method };
    if (payload !== undefined) {
      init.headers = { "Content-Type": "application/json" };
      init.body = JSON.stringify(payload);
    }
    const res = await this.request(path, init);
    return (await res.json()) as T;
  }

  health(): Promise<HealthResponse> {
    return this.json<HealthResponse>("/api/v1/health");
  }

  listDocuments(): Promise<DocumentsResponse> {
    return this.json<DocumentsResponse>("/api/v1/documents");
  }

  search(payload: SearchRequest): Promise<SearchResponse> {
    return this.json<SearchResponse>("/api/v1/search", "POST", payload);
  }

  ask(payload: AskRequest): Promise<AskResponse> {
    return this.json<AskResponse>("/api/v1/ask", "POST", payload);
  }

  async *askStream(payload: AskRequest): AsyncGenerator<StreamEvent, void, void> {
    const res = await this.request("/api/v1/ask/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(payload),
    });
    if (!res.body) {
      throw new Error("Streaming response body is empty");
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    const parseBlock = (block: string): StreamEvent | null => {
      let eventName = "";
      const dataLines: string[] = [];
      for (const line of block.split(/\r?\n/)) {
        if (line.startsWith("event:")) {
          eventName = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
          dataLines.push(line.slice(5).trim());
        }
      }
      if (!eventName || dataLines.length === 0) {
        return null;
      }
      const parsed = JSON.parse(dataLines.join("\n"));
      if (eventName === "meta" || eventName === "delta" || eventName === "done") {
        return { event: eventName, data: parsed } as StreamEvent;
      }
      return null;
    };

    while (true) {
      const { value, done } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split(/\r?\n\r?\n/);
      buffer = parts.pop() ?? "";
      for (const part of parts) {
        const ev = parseBlock(part);
        if (ev) {
          yield ev;
        }
      }
    }
    if (buffer.trim()) {
      const ev = parseBlock(buffer);
      if (ev) {
        yield ev;
      }
    }
  }

  scan(payload: ScanRequest): Promise<ScanResponse> {
    return this.json<ScanResponse>("/api/v1/scan", "POST", payload);
  }

  async fetchPageImage(docUid: string, page: number, width?: number): Promise<Uint8Array> {
    const qs = width !== undefined ? `?w=${encodeURIComponent(String(width))}` : "";
    const res = await this.request(`/api/v1/documents/${encodeURIComponent(docUid)}/pages/${page}.jpg${qs}`);
    const buf = await res.arrayBuffer();
    return new Uint8Array(buf);
  }

  agentRun(payload: AgentRunRequest): Promise<AgentRunResponse> {
    return this.json<AgentRunResponse>("/api/v1/agent/run", "POST", payload);
  }

  getApproval(approvalId: string): Promise<ApprovalRecord> {
    return this.json<ApprovalRecord>(`/api/v1/agent/approvals/${encodeURIComponent(approvalId)}`);
  }

  approveAgentAction(approvalId: string): Promise<AgentRunResponse> {
    return this.json<AgentRunResponse>(
      `/api/v1/agent/approvals/${encodeURIComponent(approvalId)}/approve`,
      "POST",
    );
  }

  rejectAgentAction(approvalId: string): Promise<AgentRejectResponse> {
    return this.json<AgentRejectResponse>(
      `/api/v1/agent/approvals/${encodeURIComponent(approvalId)}/reject`,
      "POST",
    );
  }
}
