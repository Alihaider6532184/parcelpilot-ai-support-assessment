from __future__ import annotations
import hashlib, json, re, sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
import pandas as pd
from pypdf import PdfReader

DOC_NAMES = {
    "01_Support_Policy_v3_CURRENT.pdf": ("policy", "current", None, 300, ["severity", "sla", "support"]),
    "02_Support_Policy_v2_DEPRECATED.pdf": ("policy", "deprecated", None, 0, ["severity", "sla", "support"]),
    "03_Cancellation_and_Service_Credit_SOP_v4.pdf": ("sop", "current", None, 300, ["cancellation", "service_credit"]),
    "04_Product_Operations_Guide_and_Known_Issues.pdf": ("product_guide", "current", None, 200, ["product_issue", "bulk_upload", "pickup"]),
    "05_Northstar_Logistics_Enterprise_Agreement.pdf": ("agreement", "active", "ACCT-001", 400, ["cancellation", "service_credit", "sla"]),
    "06_LumenWorks_Service_Agreement.pdf": ("agreement", "active", "ACCT-002", 400, ["cancellation", "service_credit", "sla"]),
}
INDEX_VERSION = 2

def manifest(raw_dir: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(raw_dir.iterdir()):
        if p.is_file():
            h.update(p.name.encode()); h.update(p.read_bytes())
    return h.hexdigest()

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _field(text: str, label: str) -> str:
    for line in text.splitlines():
        cleaned=_clean(line)
        if cleaned.lower().startswith(label.lower()+":"): return cleaned.split(":",1)[1].strip()
    return ""


def _section_chunks(text: str, max_words: int = 450, overlap: int = 75) -> list[tuple[str, str]]:
    """Split layout-preserving PDF text on real headings, then bound long sections."""
    blocks=[_clean(block) for block in re.split(r"\n\s*\n",text) if _clean(block)]
    sections: list[tuple[str, list[str]]] = []
    section="Document overview"; parent=""; parts: list[str]=[]

    def flush():
        nonlocal parts
        if parts: sections.append((section,parts)); parts=[]

    for block in blocks:
        numbered=re.fullmatch(r"\d+\.\s+.+",block)
        known_issue=re.fullmatch(r"KI-\d+\s+-\s+.+",block,flags=re.I)
        if numbered:
            flush(); parent=block; section=block; parts=[block]
        elif known_issue:
            flush(); section=f"{parent} > {block}" if parent else block; parts=[block]
        else:
            parts.append(block)
    flush()

    bounded=[]
    for heading, section_parts in sections:
        words=_clean(" ".join(section_parts)).split()
        if len(words) <= max_words:
            bounded.append((heading," ".join(words))); continue
        start=0
        while start < len(words):
            bounded.append((heading," ".join(words[start:start+max_words])))
            if start+max_words >= len(words): break
            start += max_words-overlap
    return bounded

def extract_documents(raw_dir: Path) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for path in sorted(raw_dir.glob("*.pdf")):
        source_type, status, account_id, rank, topics = DOC_NAMES.get(path.name, ("unknown", "current", None, 100, []))
        reader = PdfReader(path)
        for page_no, page in enumerate(reader.pages, 1):
            try: raw_text=page.extract_text(extraction_mode="layout") or ""
            except TypeError: raw_text=page.extract_text() or ""
            text = _clean(raw_text)
            if not text: continue
            account_name=_field(raw_text,"Customer")
            parts = _section_chunks(raw_text)
            for idx, (section, part) in enumerate(parts):
                chunks.append({
                    "id": f"{path.stem}-p{page_no}-c{idx}", "text": part,
                    "metadata": {"document_id": path.stem, "file_name": path.name, "page": page_no,
                        "section": section, "source_type": source_type, "status": status,
                        "account_id": account_id or "", "authority_rank": rank,
                        "account_name": account_name,
                        "topics": ",".join(topics), "effective_date": "2026-05-01" if "01_" in path.name else "2026-06-15" if "03_" in path.name else "",
                        "manifest": "", "index_version": INDEX_VERSION}
                })
    return chunks

def load_workbook(raw_dir: Path, db_path: Path) -> dict[str, Any]:
    path = raw_dir / "ParcelPilot_Assessment_Data.xlsx"
    xls = pd.ExcelFile(path)
    readme = pd.read_excel(path, sheet_name="README", header=None)
    snapshot = str(readme.iloc[1, 1])
    # Parse and validate the supplied relational data before loading it.
    frames = {name: pd.read_excel(path, sheet_name=name) for name in ("accounts", "orders", "tickets")}
    if set(frames["orders"]["account_id"]) - set(frames["accounts"]["account_id"]): raise ValueError("orphan order account")
    if set(frames["tickets"]["account_id"]) - set(frames["accounts"]["account_id"]): raise ValueError("orphan ticket account")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        for name, frame in frames.items():
            frame.to_sql(name, conn, if_exists="replace", index=False)
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{name}_account ON {name}(account_id)")
        conn.commit()
    return {"dataset_now": snapshot, "frames": frames}

def build_documents(raw_dir: Path, chroma_dir: Path) -> list[dict[str, Any]]:
    docs = extract_documents(raw_dir)
    m = manifest(raw_dir)
    for d in docs: d["metadata"]["manifest"] = m
    chroma_dir.mkdir(parents=True, exist_ok=True)
    try:
        import chromadb
        client = chromadb.PersistentClient(path=str(chroma_dir))
        try: collection=client.get_collection("parcelpilot_documents")
        except Exception: collection=client.create_collection("parcelpilot_documents",metadata={"manifest":m,"index_version":INDEX_VERSION,"hnsw:space":"cosine"})
        collection_meta=collection.metadata or {}
        if collection.count() == 0 or collection_meta.get("manifest") != m or int(collection_meta.get("index_version",0)) != INDEX_VERSION:
            try: client.delete_collection("parcelpilot_documents")
            except Exception: pass
            collection = client.create_collection("parcelpilot_documents", metadata={"manifest": m,"index_version":INDEX_VERSION,"hnsw:space":"cosine"})
            collection.add(ids=[d["id"] for d in docs], documents=[d["text"] for d in docs], metadatas=[d["metadata"] for d in docs])
    except Exception:
        pass
    # A lexical sidecar keeps local startup usable if Chroma or its embedding
    # runtime is unavailable, and makes the exact indexed units inspectable.
    (chroma_dir / "documents.json").write_text(json.dumps(docs), encoding="utf-8")
    return docs
