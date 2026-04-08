"""
OpenAI Embedding Utility
Provides functionality to generate embeddings using OpenAI's API.
Supports true batch embedding (multiple texts per API call) for performance.
"""

import os
import re
import queue
import threading
import concurrent.futures
from collections import OrderedDict
from openai import OpenAI
from typing import Dict, List, Optional
import time
import logging
from dotenv import load_dotenv

# Load environment variables from the project root
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
env_path = os.path.join(project_root, ".env")
load_dotenv(env_path)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# OpenAI client cache (thread-safe singleton)
_client: Optional[OpenAI] = None
_client_lock = threading.Lock()

# === Tokenization utilities for RAG chunking ===
try:
    import tiktoken  # type: ignore
except Exception:  # optional dependency
    tiktoken = None  # type: ignore


def _ensure_tokenizer(model: str):
    if tiktoken is None:
        raise RuntimeError(
            "tiktoken is not installed. Please `pip install tiktoken` to enable token-based chunking."
        )
    try:
        return tiktoken.encoding_for_model(model)
    except Exception:
        return tiktoken.get_encoding("cl100k_base")


def chunk_text_by_tokens(
    text: str,
    max_tokens: int = 512,
    overlap_tokens: int = 50,
    model: str = "text-embedding-3-small",
) -> List[str]:
    """
    Split text into overlapping token-aware chunks suitable for embeddings.

    Args:
        text: Input text to split
        max_tokens: Target token count per chunk
        overlap_tokens: Overlap tokens between consecutive chunks
        model: Model name to pick the appropriate tokenizer

    Returns:
        List of chunk strings
    """
    if not text:
        return []

    encoding = _ensure_tokenizer(model)
    tokens = encoding.encode(text)
    if not tokens:
        return []

    chunks: List[str] = []
    start = 0
    n = len(tokens)
    while start < n:
        end = min(start + max_tokens, n)
        chunk_tokens = tokens[start:end]
        chunks.append(encoding.decode(chunk_tokens))
        if end >= n:
            break
        # move with overlap
        start = max(0, end - overlap_tokens)
    return chunks


def chunk_text_by_sentences(
    text: str,
    max_tokens: int = 512,
    overlap_sentences: int = 1,
    model: str = "text-embedding-3-small",
) -> List[str]:
    """Split text into chunks at sentence boundaries, respecting token limits.

    Sentences are grouped until adding the next one would exceed *max_tokens*.
    The last *overlap_sentences* from the previous chunk are carried over for
    context continuity.  If a single sentence exceeds *max_tokens*, it falls
    back to :func:`chunk_text_by_tokens` for that sentence only.
    """
    if not text:
        return []

    encoding = _ensure_tokenizer(model)

    # Split on sentence-ending punctuation followed by whitespace
    sentences = re.split(r"(?<=[.!?])\s+", text)
    sentences = [s.strip() for s in sentences if s.strip()]

    if not sentences:
        return []

    chunks: List[str] = []
    current_sentences: List[str] = []
    current_tokens = 0

    for sentence in sentences:
        sent_tokens = len(encoding.encode(sentence))

        # Single sentence exceeds budget → token-split it as a fallback
        if sent_tokens > max_tokens:
            if current_sentences:
                chunks.append(" ".join(current_sentences))
                current_sentences = []
                current_tokens = 0
            chunks.extend(
                chunk_text_by_tokens(sentence, max_tokens, overlap_tokens=50, model=model)
            )
            continue

        # Adding this sentence would exceed the budget → flush
        if current_tokens + sent_tokens > max_tokens and current_sentences:
            chunks.append(" ".join(current_sentences))
            # Overlap: carry over last N sentences for context continuity
            if overlap_sentences:
                current_sentences = current_sentences[-overlap_sentences:]
                current_tokens = sum(
                    len(encoding.encode(s)) for s in current_sentences
                )
            else:
                current_sentences = []
                current_tokens = 0

        current_sentences.append(sentence)
        current_tokens += sent_tokens

    if current_sentences:
        chunks.append(" ".join(current_sentences))

    return chunks


# ---------------------------------------------------------------------------
# Contextual chunk prefixes (Anthropic-style)
# ---------------------------------------------------------------------------

_CONTEXTUAL_PREFIX_SYSTEM = (
    "You are indexing content from a college website.\n"
    "Given the full page content and a specific chunk, write a brief (2-3 sentence) "
    "description of what this chunk covers within the context of the full page.\n"
    "Include the college name and page topic. This will be prepended to the chunk "
    "before embedding to improve retrieval accuracy.\n"
    "Output ONLY the context description, nothing else."
)

_CONTEXTUAL_PREFIX_USER = (
    "College: {college_name}\n\n"
    "Full page content:\n{full_page}\n\n"
    "Chunk:\n{chunk}\n\n"
    "Context description:"
)


def generate_contextual_prefix(
    chunk: str,
    full_page: str,
    college_name: str,
    openai_client: Optional[OpenAI] = None,
) -> str:
    """Generate a 2-3 sentence contextual description to prepend to a chunk.

    Anthropic's contextual retrieval approach: gives the embedding model
    context about what this chunk covers within the full page, improving
    retrieval accuracy by ~35%.

    Args:
        chunk: The text chunk to contextualize.
        full_page: The full page content (truncated to ~3000 chars internally).
        college_name: Name of the college.
        openai_client: Optional pre-created OpenAI client.

    Returns:
        Context description string, or empty string on failure.
    """
    client = openai_client or _create_openai_client()
    if client is None:
        return ""

    try:
        response = client.chat.completions.create(
            model="gpt-4.1-nano",
            messages=[
                {"role": "system", "content": _CONTEXTUAL_PREFIX_SYSTEM},
                {
                    "role": "user",
                    "content": _CONTEXTUAL_PREFIX_USER.format(
                        college_name=college_name,
                        full_page=full_page[:3000],
                        chunk=chunk[:1000],
                    ),
                },
            ],
            temperature=0,
            max_tokens=100,
        )
        if response and response.choices:
            return response.choices[0].message.content.strip() or ""
    except Exception as exc:
        logger.debug("Contextual prefix generation failed: %s", exc)
    return ""


def _truncate_text(text: str, max_tokens: int = 8191, model: str = "text-embedding-3-small") -> str:
    """Truncate text to fit within the model's token limit using tiktoken."""
    if not text:
        return text
    if tiktoken is not None:
        encoding = _ensure_tokenizer(model)
        tokens = encoding.encode(text)
        if len(tokens) > max_tokens:
            text = encoding.decode(tokens[:max_tokens])
    elif len(text) > 8000:
        text = text[:8000]
    return text


def _create_openai_client() -> Optional[OpenAI]:
    """Create an OpenAI client after cleaning the API key from environment."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.error("OPENAI_API_KEY not found in environment.")
        return None
    # Strip quotes and whitespace that might have been added in .env
    api_key = api_key.strip().strip('"').strip("'")
    if not api_key.startswith("sk-"):
        logger.warning("OPENAI_API_KEY does not start with 'sk-'. It may be invalid.")
    return OpenAI(api_key=api_key)


def get_openai_client() -> Optional[OpenAI]:
    """Return a cached OpenAI client, creating it if necessary (thread-safe)."""
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            _client = _create_openai_client()
    return _client


# Thread-safe embedding cache (query-time only, not used during crawl)
import hashlib

_embedding_cache: OrderedDict = OrderedDict()  # LRU cache
_embedding_cache_lock = threading.Lock()
_EMBEDDING_CACHE_MAX = 1024


def get_embedding(
    text: str, model: str = "text-embedding-3-small", max_retries: int = 3
) -> Optional[List[float]]:
    """
    Generate embedding for the given text using OpenAI's embedding API.
    Results are cached in-memory (up to 1024 entries) to avoid redundant API calls.

    Args:
        text: Text to generate embedding for
        model: OpenAI embedding model to use
        max_retries: Maximum number of retry attempts

    Returns:
        List of floats representing the embedding, or None if failed
    """
    if not text or not text.strip():
        logger.warning("Empty text provided for embedding")
        return None

    text = _truncate_text(text.strip(), model=model)

    # Check cache (LRU: move hit to end)
    cache_key = hashlib.sha256((model + "|" + text).encode()).hexdigest()
    with _embedding_cache_lock:
        if cache_key in _embedding_cache:
            _embedding_cache.move_to_end(cache_key)
            logger.debug("Embedding cache hit")
            return list(_embedding_cache[cache_key])

    client = get_openai_client()
    if client is None:
        return None

    for attempt in range(max_retries):
        try:
            response = client.embeddings.create(input=text, model=model)

            if response.data and len(response.data) > 0:
                embedding = response.data[0].embedding
                logger.debug(
                    f"Successfully generated embedding of dimension {len(embedding)}"
                )
                # Store in cache (LRU eviction when over cap)
                with _embedding_cache_lock:
                    _embedding_cache[cache_key] = embedding
                    if len(_embedding_cache) > _EMBEDDING_CACHE_MAX:
                        _embedding_cache.popitem(last=False)
                return embedding
            else:
                logger.error("No embedding data returned from OpenAI")
                return None

        except Exception as e:
            if "rate_limit" in str(e).lower():
                wait_time = 2**attempt  # Exponential backoff
                logger.warning(
                    f"Rate limit hit, waiting {wait_time} seconds before retry {attempt + 1}/{max_retries}"
                )
                time.sleep(wait_time)
            else:
                logger.error(f"OpenAI API error: {e}")
                if attempt == max_retries - 1:
                    return None
                time.sleep(1)

    logger.error(f"Failed to generate embedding after {max_retries} attempts")
    return None


# ---------------------------------------------------------------------------
# Contextual chunking — generate a short context prefix for each chunk
# before embedding so the embedding captures document-level context.
# See: Anthropic Contextual Retrieval (reduces failed retrievals by ~49%).
# ---------------------------------------------------------------------------

_CONTEXT_SYSTEM = (
    "Produce a 1-2 sentence context prefix for a document chunk from a college website. "
    "The prefix should explain what this chunk covers within the larger page. "
    "Be specific about the college name and topic. Output ONLY the prefix, nothing else."
)


def generate_chunk_context(
    full_document,
    chunk,
    college_name,
    page_type,
):
    # type: (str, str, str, str) -> str
    """Generate a short context prefix for a chunk before embedding.

    Uses gpt-4.1-nano to create a 1-2 sentence description that situates
    the chunk within its source document. The prefix is prepended to the
    chunk text before embedding (but NOT stored in the content field).

    Args:
        full_document: The full page text (first 2000 chars used).
        chunk: The chunk text to contextualize.
        college_name: Name of the college this page belongs to.
        page_type: Classification of the page (about, academics, etc.).

    Returns:
        A short context string (1-2 sentences).
    """
    client = get_openai_client()
    if client is None:
        return f"From {college_name} {page_type} page."

    try:
        response = client.chat.completions.create(
            model="gpt-4.1-nano",
            messages=[
                {"role": "system", "content": _CONTEXT_SYSTEM},
                {"role": "user", "content": (
                    f"College: {college_name}\nPage type: {page_type}\n\n"
                    f"Full page (first 2000 chars):\n{full_document[:2000]}\n\n"
                    f"Chunk to contextualize:\n{chunk[:500]}"
                )},
            ],
            temperature=0,
            max_tokens=80,
            prompt_cache_key="cole-chunk-context",
        )
        if response and response.choices:
            prefix = response.choices[0].message.content
            if prefix:
                return prefix.strip()
    except Exception as exc:
        logger.debug("Contextual prefix generation failed: %s", exc)

    return f"From {college_name} {page_type} page."


def get_embeddings_batch(
    texts: List[str], model: str = "text-embedding-3-small", batch_size: int = 100,
    max_retries: int = 3,
) -> List[Optional[List[float]]]:
    """
    Generate embeddings for a batch of texts using true batch API calls.
    Sends up to `batch_size` texts per API call instead of one-at-a-time.

    Args:
        texts: List of texts to generate embeddings for
        model: OpenAI embedding model to use
        batch_size: Number of texts to process in each API call (max ~2048)
        max_retries: Maximum retry attempts per batch

    Returns:
        List of embeddings (or None for failed ones), aligned with input texts
    """
    if not texts:
        return []

    client = get_openai_client()
    if client is None:
        return [None] * len(texts)

    # Pre-process: truncate all texts to fit within token limits
    cleaned_texts = []
    valid_indices = []
    for i, text in enumerate(texts):
        if text and text.strip():
            cleaned_texts.append(_truncate_text(text.strip(), model=model))
            valid_indices.append(i)

    # Initialize results with None for empty/invalid texts
    results: List[Optional[List[float]]] = [None] * len(texts)

    # Process in sub-batches
    for batch_start in range(0, len(cleaned_texts), batch_size):
        batch = cleaned_texts[batch_start:batch_start + batch_size]
        batch_indices = valid_indices[batch_start:batch_start + batch_size]

        for attempt in range(max_retries):
            try:
                response = client.embeddings.create(input=batch, model=model)

                # Response data is returned in order of input
                for item in response.data:
                    original_idx = batch_indices[item.index]
                    results[original_idx] = item.embedding

                logger.debug(f"Batch embedded {len(batch)} texts in one API call")
                break  # Success, move to next batch

            except Exception as e:
                if "rate_limit" in str(e).lower():
                    wait_time = 2 ** attempt
                    logger.warning(
                        f"Rate limit on batch, waiting {wait_time}s (attempt {attempt + 1}/{max_retries})"
                    )
                    time.sleep(wait_time)
                elif attempt < max_retries - 1:
                    logger.warning(f"Batch embed error: {e}, retrying...")
                    time.sleep(1)
                else:
                    # Final attempt failed — fall back to individual calls for this batch
                    logger.warning(
                        f"Batch embed failed after {max_retries} attempts, falling back to individual calls"
                    )
                    for j, text in enumerate(batch):
                        emb = get_embedding(text, model=model)
                        results[batch_indices[j]] = emb

        # Small delay between sub-batches to avoid rate limits
        if batch_start + batch_size < len(cleaned_texts):
            time.sleep(0.1)

    return results


def get_chunked_embeddings_for_text(
    text: str,
    model: str = "text-embedding-3-small",
    max_tokens: int = 800,
    overlap_tokens: int = 80,
    prefix: str = "",
) -> List[Optional[List[float]]]:
    """
    Tokenize into overlapping chunks and return an embedding per chunk.

    Args:
        text: Full text to split and embed
        model: OpenAI embedding model
        max_tokens: Target chunk size in tokens
        overlap_tokens: Overlap tokens between chunks
        prefix: Optional prefix (e.g., title) to include before each chunk when embedding

    Returns:
        List of embeddings aligned with chunks
    """
    chunks = chunk_text_by_tokens(
        text, max_tokens=max_tokens, overlap_tokens=overlap_tokens, model=model
    )
    if not chunks:
        return []
    inputs = [(f"{prefix}\n\n{c}" if prefix else c) for c in chunks]
    return get_embeddings_batch(inputs, model=model)


# === Cross-thread Embedding Batcher ===

class EmbeddingBatcher:
    """Accumulates embedding requests from multiple threads and fires consolidated
    API calls, reducing round-trips when many pages complete simultaneously.

    Usage:
        batcher = EmbeddingBatcher()
        future = batcher.submit(["text1", "text2"])
        embeddings = future.result()  # blocks until batch fires
        batcher.shutdown()
    """

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        max_batch: int = 100,
        max_wait_ms: int = 200,
    ):
        self.model = model
        self.max_batch = max_batch
        self.max_wait_sec = max_wait_ms / 1000.0
        self._queue: queue.Queue = queue.Queue(maxsize=200)
        self._shutdown = threading.Event()
        self._submit_lock = threading.Lock()  # atomic check+put in submit()
        self._cancel_lock = threading.Lock()
        self._cancel_done = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="EmbeddingBatcher")
        self._thread.start()

    def submit(self, texts: List[str]) -> concurrent.futures.Future:
        """Submit a list of texts for embedding. Returns a Future resolving to
        List[Optional[List[float]]] aligned with the input texts.

        Raises RuntimeError if called after shutdown().
        """
        future: concurrent.futures.Future = concurrent.futures.Future()
        while True:
            with self._submit_lock:
                if self._shutdown.is_set():
                    raise RuntimeError("EmbeddingBatcher is shut down")
                try:
                    self._queue.put((texts, future), block=False)
                    return future
                except queue.Full:
                    pass  # release lock, let background thread drain
            time.sleep(0.05)

    def _run(self):
        """Background loop: collect submissions, fire batches."""
        while not self._shutdown.is_set():
            self._process_batch(block_timeout=0.5)
        # Drain any remaining submissions so futures don't hang on shutdown
        while not self._queue.empty():
            self._process_batch(block_timeout=0.1)
        # Final sweep: cancel any futures that slipped in during the drain
        # (TOCTOU: a submit() could sneak between the empty() check and here)
        self._cancel_remaining()

    def _process_batch(self, block_timeout: float = 0.5):
        """Collect pending submissions and fire a single batched API call."""
        pending = []  # list of (future, start_idx, count)
        all_texts = []

        # Block until at least one submission arrives
        try:
            texts, future = self._queue.get(timeout=block_timeout)
            start_idx = len(all_texts)
            all_texts.extend(texts)
            pending.append((future, start_idx, len(texts)))
        except queue.Empty:
            return

        # Drain more items up to max_batch or max_wait
        deadline = time.monotonic() + self.max_wait_sec
        while len(all_texts) < self.max_batch and time.monotonic() < deadline:
            try:
                remaining = max(0.01, deadline - time.monotonic())
                texts, future = self._queue.get(timeout=remaining)
                start_idx = len(all_texts)
                all_texts.extend(texts)
                pending.append((future, start_idx, len(texts)))
            except queue.Empty:
                break

        # Fire the batch
        try:
            embeddings = get_embeddings_batch(all_texts, model=self.model)
            for future, start_idx, count in pending:
                future.set_result(embeddings[start_idx:start_idx + count])
        except Exception as e:
            for future, _, _ in pending:
                if not future.done():
                    future.set_exception(e)

    def _cancel_remaining(self):
        """Cancel any futures left in the queue so callers don't hang.

        Guarded by ``_cancel_lock`` so this runs exactly once even when called
        from both the background thread (end of ``_run``) and the main thread
        (``shutdown``).
        """
        with self._cancel_lock:
            if self._cancel_done:
                return
            self._cancel_done = True
        # Safe to drain — only one thread reaches here
        cancelled = 0
        while True:
            try:
                _texts, future = self._queue.get_nowait()
                if not future.done():
                    future.set_exception(
                        RuntimeError("EmbeddingBatcher shut down before processing")
                    )
                cancelled += 1
            except queue.Empty:
                break
        if cancelled:
            print(f"    ⚠️  EmbeddingBatcher cancelled {cancelled} pending request(s) on shutdown")

    def shutdown(self):
        """Signal the background thread to stop and wait for it to drain."""
        self._shutdown.set()
        # Fence: any in-flight submit() either completes its put() before we
        # proceed (so _run()'s drain will see it), or sees _shutdown=True and
        # raises (so no orphaned future).
        with self._submit_lock:
            pass
        self._thread.join(timeout=15)
        if self._thread.is_alive():
            print("    ⚠️  EmbeddingBatcher did not stop within 15s")
        # Cancel anything the background thread didn't get to
        self._cancel_remaining()
