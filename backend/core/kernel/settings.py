"""Settings - every start-up value the backend reads from the environment, in one place.

Before Phase 3 these were spread through app.py (DATA_DIR, APP_DIR, EMBED_MODEL, the CORS rule, VR_RAG_NO_DB).
Defaults are exactly the old values, so nothing changes unless an environment variable is set.

    VR_RAG_DATA_DIR     document store + app data folder        (default: <root>/backend/data)
    VR_RAG_NO_DB=1      never connect to Postgres, use local files
    VR_RAG_EMBED_MODEL  fastembed model name                    (default: BAAI/bge-small-en-v1.5)
    VR_RAG_CORS_REGEX   allowed browser origins (regex)         (default: localhost, *.vercel.app, *.onrender.com)
    PORT                set by Render; also means "server mode" (bind 0.0.0.0, no browser)
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

DEFAULT_EMBED_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_CORS_REGEX = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$|^https://.*\.vercel\.app$|^https://.*\.onrender\.com$"


@dataclass(frozen=True)
class Settings:
    root: Path                      # repository root
    data_dir: Path                  # uploads, page images, local app data
    seed_dir: Path                  # documents imported on the very first run
    app_dir: Path                   # static front-end served at "/"
    embed_model: str
    no_db: bool
    cors_origin_regex: str
    port: Optional[int]             # from $PORT (Render); None when run locally

    @property
    def backend_dir(self) -> Path:
        return self.root / "backend"

    @classmethod
    def from_env(cls, root: Path, argv: Sequence[str] = (), env: Optional[Mapping[str, str]] = None) -> "Settings":
        env = {} if env is None else env
        root = Path(root).resolve()
        data = env.get("VR_RAG_DATA_DIR") or ""
        port = env.get("PORT") or ""
        return cls(
            root=root,
            data_dir=Path(data) if data else root / "backend" / "data",
            seed_dir=root / "docs",
            app_dir=root / ("web" if "--app=web" in argv else "glasses_app/dist"),
            embed_model=env.get("VR_RAG_EMBED_MODEL", DEFAULT_EMBED_MODEL),
            no_db=env.get("VR_RAG_NO_DB") == "1",
            cors_origin_regex=env.get("VR_RAG_CORS_REGEX") or DEFAULT_CORS_REGEX,
            port=int(port) if port.isdigit() else None,
        )
