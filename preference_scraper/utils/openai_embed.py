"""
OpenAI Embedding Utility
Provides functionality to generate embeddings using OpenAI's API.
Supports true batch embedding (multiple texts per API call) for performance.
"""

import os
import queue
import threading
import concurrent.futures
from openai import OpenAI
from typing import List, Optional
import time
import logging
from dotenv import load_dotenv

# Load environment variables from the project root
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
env_path = os.path.join(project_root, ".env")
load_dotenv(env_path)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# OpenAI client cache
_client: Optional[OpenAI] = None

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
    max_tokens: int = 800,
    overlap_tokens: int = 80,
    model: str = "text-embedding-ada-002",
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


def _truncate_text(text: str, max_tokens: int = 8191, model: str = "text-embedding-ada-002") -> str:
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
    """Return a cached OpenAI client, creating it if necessary."""
    global _client
    if _client is None:
        _client = _create_openai_client()
    return _client


def get_embedding(
    text: str, model: str = "text-embedding-ada-002", max_retries: int = 3
) -> Optional[List[float]]:
    """
    Generate embedding for the given text using OpenAI's embedding API.

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

    client = get_openai_client()
    if client is None:
        return None

    text = _truncate_text(text.strip(), model=model)

    for attempt in range(max_retries):
        try:
            response = client.embeddings.create(input=text, model=model)

            if response.data and len(response.data) > 0:
                embedding = response.data[0].embedding
                logger.debug(
                    f"Successfully generated embedding of dimension {len(embedding)}"
                )
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


def get_embeddings_batch(
    texts: List[str], model: str = "text-embedding-ada-002", batch_size: int = 100,
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
    model: str = "text-embedding-ada-002",
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
        model: str = "text-embedding-ada-002",
        max_batch: int = 100,
        max_wait_ms: int = 200,
    ):
        self.model = model
        self.max_batch = max_batch
        self.max_wait_sec = max_wait_ms / 1000.0
        self._queue: queue.Queue = queue.Queue()
        self._shutdown = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="EmbeddingBatcher")
        self._thread.start()

    def submit(self, texts: List[str]) -> concurrent.futures.Future:
        """Submit a list of texts for embedding. Returns a Future resolving to
        List[Optional[List[float]]] aligned with the input texts."""
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._queue.put((texts, future))
        return future

    def _run(self):
        """Background loop: collect submissions, fire batches."""
        while not self._shutdown.is_set():
            pending = []  # list of (texts, future, start_idx, count)
            all_texts = []

            # Block until at least one submission arrives
            try:
                texts, future = self._queue.get(timeout=0.5)
                start_idx = len(all_texts)
                all_texts.extend(texts)
                pending.append((future, start_idx, len(texts)))
            except queue.Empty:
                continue

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

    def shutdown(self):
        """Signal the background thread to stop and wait for it."""
        self._shutdown.set()
        self._thread.join(timeout=5)
