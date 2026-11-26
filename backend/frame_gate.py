"""frame_gate.py - lightweight blur & perceptual-hash (dHash) gating for smart-glasses frames.

Uses only Pillow, NumPy, and the Python standard library (no OpenCV).
"""
import io
import os
from dataclasses import dataclass
from typing import Optional

import numpy as np
from PIL import Image


def blur_threshold_default() -> float:
    try:
        return float(os.environ.get("VR_RAG_BLUR_THRESHOLD", "60.0"))
    except ValueError:
        return 60.0


def dedup_hamming_default() -> int:
    try:
        return int(os.environ.get("VR_RAG_DEDUP_HAMMING", "6"))
    except ValueError:
        return 6


@dataclass(frozen=True)
class FrameGateResult:
    blur_score: float
    dhash: int
    hamming_distance: Optional[int]
    is_blurry: bool
    is_duplicate: bool

    def to_dict(self) -> dict:
        return {
            "blur_score": round(self.blur_score, 2),
            "dhash": f"{self.dhash:016x}",
            "hamming_distance": self.hamming_distance,
            "is_blurry": self.is_blurry,
            "is_duplicate": self.is_duplicate,
        }


def compute_blur_score(img: Image.Image) -> float:
    """Grayscale, downscale longest side <= 512px, return 3x3 discrete Laplacian variance."""
    gray = img.convert("L")
    if max(gray.size) > 512:
        gray = gray.copy()
        gray.thumbnail((512, 512), Image.Resampling.BILINEAR)
    arr = np.asarray(gray, dtype=np.float64)
    if arr.shape[0] < 3 or arr.shape[1] < 3:
        return 0.0
    lap = (
        arr[:-2, 1:-1]
        + arr[2:, 1:-1]
        + arr[1:-1, :-2]
        + arr[1:-1, 2:]
        - 4.0 * arr[1:-1, 1:-1]
    )
    return float(np.var(lap))


def compute_dhash(img: Image.Image) -> int:
    """Convert to grayscale, resize to 9x8, and compute 64-bit horizontal difference hash."""
    resized = img.convert("L").resize((9, 8), Image.Resampling.BILINEAR)
    arr = np.asarray(resized, dtype=np.int16)
    diff = arr[:, 1:] > arr[:, :-1]
    h = 0
    for bit in diff.ravel():
        h = (h << 1) | int(bool(bit))
    return h


def hamming_distance(h1: int, h2: int) -> int:
    """Bitwise Hamming distance between two 64-bit hashes."""
    return (int(h1) ^ int(h2)).bit_count()


def evaluate_frame(
    image_bytes: bytes,
    last_dhash: Optional[int] = None,
    blur_threshold: Optional[float] = None,
    dedup_hamming: Optional[int] = None,
) -> FrameGateResult:
    """Decode image bytes and evaluate blur + perceptual-hash deduplication."""
    with Image.open(io.BytesIO(image_bytes)) as im:
        im.load()
        blur_score = compute_blur_score(im)
        dhash = compute_dhash(im)

    b_thresh = blur_threshold_default() if blur_threshold is None else float(blur_threshold)
    h_thresh = dedup_hamming_default() if dedup_hamming is None else int(dedup_hamming)
    dist = hamming_distance(dhash, last_dhash) if last_dhash is not None else None
    is_blurry = blur_score < b_thresh
    is_duplicate = (not is_blurry) and (dist is not None) and (dist <= h_thresh)
    return FrameGateResult(
        blur_score=blur_score,
        dhash=dhash,
        hamming_distance=dist,
        is_blurry=is_blurry,
        is_duplicate=is_duplicate,
    )
