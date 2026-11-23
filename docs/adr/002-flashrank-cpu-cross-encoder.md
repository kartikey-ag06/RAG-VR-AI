# ADR 002: FlashRank cross-encoder reranking

- **Status:** accepted
- **Context:** RRF mixes ranks but does not read query and passage together, so loosely related chunks reach the prompt.
- **Decision:** re-score the top 20 hybrid candidates with FlashRank (`ms-marco-TinyBERT-L-2-v2`, ONNX, CPU, downloaded once) and keep the best 8. The query is scored against `document title. chunk`. The top score is returned as `confidence` (high >= 0.75, medium >= 0.35, low below).
- **Consequences:** about 0.15 s extra per search. The score is noisy on short keyword queries, so a question is only treated as "not in the documents" when the rerank score is below 0.20 AND the best embedding similarity is below 0.65 (`VR_RAG_MIN_CONFIDENCE`, `VR_RAG_MIN_SEMANTIC`). Re-check with `backend/test_rag_eval.py` after any change.
