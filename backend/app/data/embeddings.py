from __future__ import annotations

import hashlib
import math
import re


DIMENSIONS = 384


def _index_and_sign(feature: str) -> tuple[int, float]:
    digest=hashlib.blake2b(feature.encode("utf-8"),digest_size=8).digest()
    value=int.from_bytes(digest,"little")
    return value % DIMENSIONS, 1.0 if value & (1 << 63) else -1.0


def embed_text(text: str) -> list[float]:
    """Return a stable, dependency-free feature-hashed text embedding.

    Word, adjacent-word, and character features give Chroma a useful first-pass
    similarity signal without loading a second 79 MB ONNX model per process.
    The lexical ranker still performs the final domain-aware reranking.
    """
    words=re.findall(r"[a-z0-9]+",text.lower())
    vector=[0.0]*DIMENSIONS

    def add(feature: str, weight: float) -> None:
        index,sign=_index_and_sign(feature); vector[index]+=sign*weight

    for word in words:
        add("w:"+word,1.0)
        padded=f"^{word}$"
        for i in range(max(0,len(padded)-2)): add("c:"+padded[i:i+3],0.18)
    for left,right in zip(words,words[1:]): add(f"b:{left}:{right}",0.55)
    norm=math.sqrt(sum(value*value for value in vector)) or 1.0
    return [value/norm for value in vector]


def embed_texts(texts: list[str]) -> list[list[float]]:
    return [embed_text(text) for text in texts]
