"""Permission-aware hybrid retrieval.

Three ideas carry this file.

**The filter comes before the search, not after.** A principal's access level
narrows the candidate set before anything is scored, so a restricted chunk
cannot reach the model at all. Filtering a generated answer is too late: the
confidential text has already been in the prompt, and "the model was told not
to repeat it" is not an access control. A test asserts this directly.

**Chunks carry their heading.** A clause reads as "Master Services Agreement
— Meridian > 4.2 Rate increases > ..." so both the lexical index and the
model see what the passage *is*, not just what it says. Retrieval quality on
contract text is mostly a chunking problem.

**Two retrievers, fused.** BM25 catches exact terms that matter here --
clause numbers, document ids, "fuel surcharge" -- and embeddings catch the
paraphrase, because a reviewer asks "can they put the price up?" and the
contract says "rate increases". Neither alone is good enough. Reciprocal rank
fusion combines them without needing the two score scales to be comparable,
which they are not.

Embeddings come from OpenRouter and are cached on disk by content hash.
Without a key the dense half is skipped and search degrades to BM25 alone,
reporting that it did so rather than pretending otherwise.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .corpus import DOCUMENTS, DOCUMENTS_BY_ID, Document, Principal

CACHE_PATH = os.environ.get("EMBEDDING_CACHE", os.path.join("data", "embeddings.json"))
DEFAULT_EMBED_MODEL = os.environ.get("OPENROUTER_EMBED_MODEL", "openai/text-embedding-3-small")

# The corpus is dated around this point, matching the invoice data.
DEFAULT_AS_OF = "2026-06-01"

# A document that has stopped applying is demoted, not removed. Removed, and
# "what was the rate before the increase?" becomes unanswerable. Left at full
# weight, a superseded rate card outranks the current one on a query about
# current rates -- it uses the same words and is often the tidier document.
# Demotion keeps both questions answerable and lets the ranking carry the
# distinction that the words do not.
STALE_PENALTY = 0.70
FUTURE_PENALTY = 0.80

# ...and only where a date range actually means "this governs now". On a
# contract, a rate card or a policy, effective_from and effective_to bound
# when the document has force. On an email, a dispute record or a delivery
# note, the same field is just when it happened -- demoting a dispute for
# being recent would be exactly backwards.
VALIDITY_TYPES = {"contract", "rate_card", "policy"}

# ...and not at all when the question is explicitly about the past. "What is
# the current rate?" and "What was the rate before the increase?" want
# opposite things from the same two documents, and the only signal available
# at query time is the wording.
#
# This is a heuristic and should be read as one. The better answer, which the
# agent's search tool can use, is to pass the date that actually governs --
# the date the purchase order was raised -- instead of inferring intent from
# words. The heuristic is the fallback for questions that arrive without one.
HISTORICAL_MARKERS = (
    "before", "previous", "prior", "was ", "were ", "used to", "old ",
    "earlier", "superseded", "back then", "at the time", "history",
    "historical", "last year", "originally",
)


def historical_intent(query: str) -> bool:
    q = f" {(query or '').lower()} "
    return any(m in q for m in HISTORICAL_MARKERS)

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have",
    "in", "is", "it", "its", "of", "on", "or", "that", "the", "this", "to", "was",
    "were", "will", "with", "shall", "may", "any", "not", "no", "we", "our", "i",
    "do", "does", "if", "can", "what", "which", "who", "how", "when", "where",
}


def tokenize(text: str) -> List[str]:
    """Lowercase word tokens, with one deliberate exception: strings like
    '4.2' and 'DN441201' survive intact, because on contract text the clause
    number is often the whole query."""
    raw = re.findall(r"[a-z0-9][a-z0-9._-]*", (text or "").lower())
    out = []
    for t in raw:
        t = t.strip("._-")
        if not t or t in STOPWORDS or len(t) == 1:
            continue
        out.append(t)
    return out


# --------------------------------------------------------------------------
# chunking
# --------------------------------------------------------------------------


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    heading: str
    text: str
    ordinal: int

    @property
    def doc(self) -> Document:
        return DOCUMENTS_BY_ID[self.doc_id]

    @property
    def citation(self) -> str:
        return f"{self.doc_id}§{self.heading}" if self.heading else self.doc_id

    def indexed_text(self) -> str:
        return f"{self.doc.title} > {self.heading}\n{self.text}" if self.heading else \
               f"{self.doc.title}\n{self.text}"

    def to_dict(self, include_text: bool = True) -> Dict[str, Any]:
        d = {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "title": self.doc.title,
            "heading": self.heading,
            "citation": self.citation,
            "doc_type": self.doc.doc_type,
            "vendor": self.doc.vendor,
            "effective_from": self.doc.effective_from,
            "effective_to": self.doc.effective_to,
            "authority": self.doc.authority,
            "access_level": self.doc.access_level,
            "shareable": self.doc.shareable,
        }
        if include_text:
            d["text"] = self.text
        return d


CLAUSE_RE = re.compile(r"^\s*(\d+\.\d+|\d+\.)\s+([A-Z][^.\n]{0,70}?)[.\n]")
EMAIL_SPLIT = re.compile(r"\n-{3,}\n")


def chunk_document(doc: Document, target_chars: int = 900) -> List[Chunk]:
    """Split along the document's own seams.

    Contracts split at clause numbers, email threads at message boundaries,
    everything else at blank lines with a size cap. Splitting a contract every
    N characters would cut clause 4.2 in half and retrieve neither piece well.
    """
    body = doc.body.strip("\n")
    pieces: List[Tuple[str, str]] = []          # (heading, text)

    if doc.doc_type == "correspondence":
        for i, part in enumerate(EMAIL_SPLIT.split(body), start=1):
            part = part.strip()
            if not part:
                continue
            m = re.search(r"^Date:\s*(.+)$", part, re.M)
            head = f"message {i}, {m.group(1).strip()}" if m else f"message {i}"
            if part.lstrip().startswith("AP note"):
                head += " (AP note)"
            pieces.append((head, part))

    elif doc.doc_type in ("contract", "policy"):
        # Policies are numbered the same way contracts are, and a reviewer
        # wants "POL-AP-001 section 5", not "the AP policy". Citing at the
        # clause is only possible if the chunk is the clause.
        cur_head, buf = "", []
        for line in body.split("\n"):
            m = CLAUSE_RE.match(line)
            if m:
                if buf and "".join(buf).strip():
                    pieces.append((cur_head, "\n".join(buf).strip()))
                cur_head = f"{m.group(1).rstrip('.')} {m.group(2).strip()}"
                buf = [line]
            else:
                buf.append(line)
        if buf and "".join(buf).strip():
            pieces.append((cur_head, "\n".join(buf).strip()))
        # A policy with no numbered clauses (POL-AP-012) produces one
        # headless piece here, which is correct: its citation is the
        # document id, and that is what the agent will be handed.

    else:
        block, size = [], 0
        for para in re.split(r"\n\s*\n", body):
            para = para.strip()
            if not para:
                continue
            if size + len(para) > target_chars and block:
                pieces.append(("", "\n\n".join(block)))
                block, size = [], 0
            block.append(para)
            size += len(para)
        if block:
            pieces.append(("", "\n\n".join(block)))

    return [
        Chunk(chunk_id=f"{doc.doc_id}#{i}", doc_id=doc.doc_id,
              heading=h, text=t, ordinal=i)
        for i, (h, t) in enumerate(pieces, start=1)
        if t.strip()
    ]


def build_chunks(documents: Optional[List[Document]] = None) -> List[Chunk]:
    out: List[Chunk] = []
    for doc in documents if documents is not None else DOCUMENTS:
        out.extend(chunk_document(doc))
    return out


# --------------------------------------------------------------------------
# BM25
# --------------------------------------------------------------------------


class BM25:
    """Okapi BM25. Written out rather than imported so the scoring is visible:
    term frequency saturates (k1), long passages are penalised (b), and rare
    terms weigh more (idf). Those three behaviours are the whole algorithm."""

    def __init__(self, chunks: List[Chunk], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.chunks = chunks
        self.docs: List[List[str]] = [tokenize(c.indexed_text()) for c in chunks]
        self.lengths = [len(d) for d in self.docs]
        self.avg_len = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0
        self.freqs: List[Dict[str, int]] = []
        df: Dict[str, int] = {}
        for d in self.docs:
            f: Dict[str, int] = {}
            for t in d:
                f[t] = f.get(t, 0) + 1
            self.freqs.append(f)
            for t in f:
                df[t] = df.get(t, 0) + 1
        n = len(self.docs) or 1
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def scores(self, query: str, allowed: Optional[set] = None) -> List[Tuple[int, float]]:
        q = tokenize(query)
        out: List[Tuple[int, float]] = []
        for i, f in enumerate(self.freqs):
            if allowed is not None and i not in allowed:
                continue
            s = 0.0
            for t in q:
                tf = f.get(t)
                if not tf:
                    continue
                denom = tf + self.k1 * (1 - self.b + self.b * self.lengths[i] / (self.avg_len or 1))
                s += self.idf.get(t, 0.0) * tf * (self.k1 + 1) / denom
            if s > 0:
                out.append((i, s))
        out.sort(key=lambda kv: -kv[1])
        return out


# --------------------------------------------------------------------------
# embeddings
# --------------------------------------------------------------------------


class EmbeddingClient:
    """OpenRouter embeddings over urllib, cached on disk by content hash.

    The cache matters for more than cost: it makes retrieval reproducible
    between runs, which is what lets the eval numbers mean anything.
    """

    ENDPOINT = "https://openrouter.ai/api/v1/embeddings"

    def __init__(self, model: str = DEFAULT_EMBED_MODEL, api_key: Optional[str] = None,
                 cache_path: str = CACHE_PATH, timeout: int = 60) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self.cache_path = cache_path
        self.timeout = timeout
        self._lock = threading.Lock()
        self._cache: Dict[str, List[float]] = {}
        self._load()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self.model}\x00{text}".encode()).hexdigest()[:32]

    def _load(self) -> None:
        try:
            with open(self.cache_path, encoding="utf-8") as fh:
                self._cache = json.load(fh)
        except Exception:
            self._cache = {}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.cache_path) or ".", exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as fh:
                json.dump(self._cache, fh)
        except Exception:
            pass        # a cache that cannot be written is slow, not broken

    def embed(self, texts: List[str]) -> Optional[List[List[float]]]:
        if not self.enabled:
            return None
        missing = [t for t in texts if self._key(t) not in self._cache]
        if missing:
            for i in range(0, len(missing), 64):
                batch = missing[i:i + 64]
                vectors = self._call(batch)
                if vectors is None:
                    return None
                with self._lock:
                    for t, v in zip(batch, vectors):
                        self._cache[self._key(t)] = v
            self._save()
        return [self._cache[self._key(t)] for t in texts]

    def _call(self, batch: List[str]) -> Optional[List[List[float]]]:
        req = urllib.request.Request(
            self.ENDPOINT,
            data=json.dumps({"model": self.model, "input": batch}).encode(),
            headers={"content-type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode())
        except Exception:
            return None
        rows = sorted(body.get("data") or [], key=lambda r: r.get("index", 0))
        vecs = [r.get("embedding") for r in rows]
        return vecs if len(vecs) == len(batch) and all(vecs) else None


def cosine(a: List[float], b: List[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return num / (na * nb) if na and nb else 0.0


# --------------------------------------------------------------------------
# the retriever
# --------------------------------------------------------------------------


@dataclass
class Hit:
    chunk: Chunk
    score: float
    bm25_rank: Optional[int] = None
    dense_rank: Optional[int] = None
    stale: bool = False

    def to_dict(self, include_text: bool = True) -> Dict[str, Any]:
        d = self.chunk.to_dict(include_text)
        d["score"] = round(self.score, 5)
        d["bm25_rank"] = self.bm25_rank
        d["dense_rank"] = self.dense_rank
        d["stale"] = self.stale
        return d


@dataclass
class SearchResult:
    hits: List[Hit]
    mode: str                    # hybrid | lexical
    considered: int              # chunks the principal was allowed to see
    withheld: int                # chunks excluded by access level
    query: str

    def to_dict(self, include_text: bool = True) -> Dict[str, Any]:
        return {
            "query": self.query,
            "mode": self.mode,
            "considered": self.considered,
            "withheld": self.withheld,
            "hits": [h.to_dict(include_text) for h in self.hits],
        }


class Retriever:
    def __init__(self, chunks: Optional[List[Chunk]] = None,
                 embedder: Optional[EmbeddingClient] = None,
                 as_of: Optional[str] = DEFAULT_AS_OF) -> None:
        self.as_of = as_of
        self.chunks = chunks if chunks is not None else build_chunks()
        self.bm25 = BM25(self.chunks)
        self.embedder = embedder if embedder is not None else EmbeddingClient()
        self._vectors: Optional[List[List[float]]] = None
        self._vec_lock = threading.Lock()

    # -- dense half --------------------------------------------------------

    def _ensure_vectors(self) -> bool:
        if self._vectors is not None:
            return True
        if not self.embedder.enabled:
            return False
        with self._vec_lock:
            if self._vectors is None:
                vecs = self.embedder.embed([c.indexed_text() for c in self.chunks])
                if vecs is None:
                    return False
                self._vectors = vecs
        return True

    # -- access control ----------------------------------------------------

    def _allowed_indices(self, principal: Principal,
                         vendor: Optional[str] = None,
                         doc_types: Optional[List[str]] = None) -> Tuple[set, int]:
        """Runs before any scoring. Returns the permitted index set and how
        many chunks were withheld on access grounds alone."""
        allowed, withheld = set(), 0
        for i, c in enumerate(self.chunks):
            if not principal.may_see(c.doc):
                withheld += 1
                continue
            if vendor and c.doc.vendor and c.doc.vendor != vendor:
                continue
            if vendor and c.doc.vendor is None and doc_types is None:
                pass        # company-wide documents stay in scope
            if doc_types and c.doc.doc_type not in doc_types:
                continue
            allowed.add(i)
        return allowed, withheld

    # -- search ------------------------------------------------------------

    def search(
        self,
        query: str,
        principal: Principal,
        k: int = 6,
        vendor: Optional[str] = None,
        doc_types: Optional[List[str]] = None,
        candidates: int = 24,
        rrf_k: int = 60,
        as_of: Optional[str] = "__default__",
    ) -> SearchResult:
        allowed, withheld = self._allowed_indices(principal, vendor, doc_types)
        if not allowed:
            return SearchResult([], "lexical", 0, withheld, query)

        lexical = self.bm25.scores(query, allowed)[:candidates]
        lex_rank = {idx: r for r, (idx, _) in enumerate(lexical, start=1)}

        dense_rank: Dict[int, int] = {}
        mode = "lexical"
        if self._ensure_vectors():
            qv = self.embedder.embed([query])
            if qv:
                sims = sorted(
                    ((i, cosine(qv[0], self._vectors[i])) for i in allowed),
                    key=lambda kv: -kv[1],
                )[:candidates]
                dense_rank = {idx: r for r, (idx, _) in enumerate(sims, start=1)}
                mode = "hybrid"

        # Reciprocal rank fusion: combine by position, not by score, because
        # a BM25 score and a cosine similarity are not on the same scale and
        # normalising them is a fudge that breaks on the next corpus.
        fused: Dict[int, float] = {}
        for idx, r in lex_rank.items():
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (rrf_k + r)
        for idx, r in dense_rank.items():
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (rrf_k + r)

        # Validity prior: demote what no longer applies, and what does not
        # apply yet, without removing either from reach.
        when = self.as_of if as_of == "__default__" else as_of
        if when and historical_intent(query):
            when = None          # the question is about the past; do not demote it
        if when:
            for idx in list(fused):
                doc = self.chunks[idx].doc
                if doc.doc_type not in VALIDITY_TYPES:
                    continue
                if doc.effective_to and doc.effective_to < when:
                    fused[idx] *= STALE_PENALTY
                elif doc.effective_from and doc.effective_from > when:
                    fused[idx] *= FUTURE_PENALTY

        ranked = sorted(fused.items(), key=lambda kv: -kv[1])[:k]
        hits = [
            Hit(chunk=self.chunks[i], score=s,
                bm25_rank=lex_rank.get(i), dense_rank=dense_rank.get(i),
                stale=bool(when and self.chunks[i].doc.doc_type in VALIDITY_TYPES
                           and self.chunks[i].doc.effective_to
                           and self.chunks[i].doc.effective_to < when))
            for i, s in ranked
        ]
        return SearchResult(hits, mode, len(allowed), withheld, query)

    # -- whole documents ---------------------------------------------------

    def read(self, doc_id: str, principal: Principal) -> Optional[Dict[str, Any]]:
        doc = DOCUMENTS_BY_ID.get(doc_id)
        if doc is None:
            return None
        if not principal.may_see(doc):
            # Absent rather than forbidden: telling a clerk that a legal note
            # about this vendor exists is itself a disclosure.
            return None
        return {
            **doc.to_dict(),
            "sections": [
                {"heading": c.heading, "citation": c.citation, "text": c.text}
                for c in chunk_document(doc)
            ],
        }
