# `@rag-vr-ai/sdk` (TypeScript)

Zero-dependency typed client for the RAG-VR-AI `/api/v1` API (Node 18+, browsers, React Native / Expo, and Meta Ray-Ban Display Web Apps).

## Quickstart

```ts
import { RagVrClient, RagVrApiError } from "@rag-vr-ai/sdk";

const client = new RagVrClient({
  baseUrl: "http://localhost:8000",
  token: process.env.RAG_VR_TOKEN, // "vrk_..." -> Authorization: Bearer, otherwise X-VR-Token
});

const health = await client.health();
const answer = await client.ask({ question: "What is the torque spec?", style: "glance" });

for await (const ev of client.askStream({ question: "Summarize safety steps", style: "spoken" })) {
  if (ev.event === "delta") {
    process.stdout.write(ev.data.text);
  }
}
```
