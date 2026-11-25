"""
Build ChromaDB Vector Database for AP IGRS Statutory Knowledge Base.

Embeds 257 statutory chunks from Indian Stamp Act 1899, Registration Act 1908,
and C&IG Circulars into a persistent ChromaDB vector store.
"""
import sqlite3
import os
from pathlib import Path
import chromadb
from chromadb.utils import embedding_functions

ROOT_DIR = Path(__file__).resolve().parent.parent
SQLITE_DB = ROOT_DIR / "data" / "legal_knowledge.db"
CHROMA_DIR = ROOT_DIR / "data" / "chroma_db"


def build_chroma_vector_store():
    print(f"[Vector DB] Connecting to SQLite knowledge base: {SQLITE_DB}")
    if not SQLITE_DB.exists():
        raise FileNotFoundError(f"Knowledge base not found at {SQLITE_DB}")

    conn = sqlite3.connect(str(SQLITE_DB))
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, act_name, section_or_rule, title, content, sro_guidance, authority_citation
        FROM legal_chunks
    """)
    rows = cursor.fetchall()
    conn.close()

    print(f"[Vector DB] Loaded {len(rows)} statutory chunks from SQLite.")

    # Initialize Chroma Persistent Client
    os.makedirs(str(CHROMA_DIR), exist_ok=True)
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    
    # Use default embedding function (ONNX all-MiniLM-L6-v2)
    collection_name = "ap_statutory_rules"
    try:
        client.delete_collection(name=collection_name)
    except Exception:
        pass

    collection = client.create_collection(
        name=collection_name,
        metadata={"description": "AP Registration & Stamp Act Statutory Knowledge Base"}
    )

    documents = []
    metadatas = []
    ids = []

    for r in rows:
        c_id, act_name, section_or_rule, title, content, sro_guidance, authority_citation = r
        
        # Enriched semantic document representation for vector space
        doc_text = (
            f"Statutory Section: {section_or_rule} - {title}\n"
            f"Act: {act_name}\n"
            f"SRO Guidance Directive: {sro_guidance}\n"
            f"Statutory Provisions: {content[:1500]}"
        )
        
        documents.append(doc_text)
        metadatas.append({
            "chunk_id": str(c_id),
            "act_name": str(act_name or ""),
            "section_or_rule": str(section_or_rule or ""),
            "title": str(title or ""),
            "authority_citation": str(authority_citation or ""),
            "sro_guidance": str(sro_guidance or "")
        })
        ids.append(f"chunk_{c_id}")

    # Add in batches of 50
    batch_size = 50
    for i in range(0, len(documents), batch_size):
        end = min(i + batch_size, len(documents))
        collection.add(
            documents=documents[i:end],
            metadatas=metadatas[i:end],
            ids=ids[i:end]
        )
        print(f"[Vector DB] Indexed batch {i+1} to {end} / {len(documents)}")

    count = collection.count()
    print(f"[Vector DB] Successfully indexed {count} vectors into ChromaDB at {CHROMA_DIR}!")

    # Verify semantic retrieval
    test_queries = [
        "prohibited assigned land registration refusal",
        "recovery of deficit stamp duty under Section 41-A",
        "time limit to present document for registration within 4 months"
    ]
    print("\n--- Semantic Retrieval Sanity Check ---")
    for q in test_queries:
        res = collection.query(query_texts=[q], n_results=1)
        top_meta = res["metadatas"][0][0]
        print(f"Query: '{q}' -> Top Match: {top_meta['section_or_rule']} ({top_meta['title']})")


if __name__ == "__main__":
    build_chroma_vector_store()
