# ADR 001: Embedded Qdrant for vector storage

- **Status:** accepted
- **Context:** vectors lived in NumPy arrays plus per-document `.npy` files. That is not queryable, not shared between processes, and gets slower as the corpus grows. ChromaDB was tried first and replaced on request.
- **Decision:** use `qdrant-client` in embedded mode (`QdrantClient(path="backend/data/qdrant")`). No server to install. One cosine collection per embedding model. Point ids are `uuid5(doc uid : chunk position : sha1 of embedded text)`, so a restart reuses stored vectors, edits replace them, and deleted documents are removed. On first start the store is created automatically.
- **Consequences:** only one process may open the folder (no multi-worker deployment without moving to the Qdrant server: change the client line only). Embedded mode scans linearly, which is fine for thousands of chunks. If Qdrant fails to load, the old `.npy` cache is used.
