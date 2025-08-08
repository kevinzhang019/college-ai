"""
OpenAI Embedding Utility
Provides functionality to generate embeddings using OpenAI's API.
"""

import os
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

    # Clean and truncate text if too long
    text = text.strip()
    if len(text) > 8000:  # OpenAI embedding limit is around 8191 tokens
        text = text[:8000]
        logger.warning("Text truncated to 8000 characters for embedding")

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
    texts: List[str], model: str = "text-embedding-ada-002", batch_size: int = 100
) -> List[Optional[List[float]]]:
    """
    Generate embeddings for a batch of texts.

    Args:
        texts: List of texts to generate embeddings for
        model: OpenAI embedding model to use
        batch_size: Number of texts to process in each batch

    Returns:
        List of embeddings (or None for failed ones)
    """
    embeddings = []

    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        batch_embeddings = []

        for text in batch:
            embedding = get_embedding(text, model)
            batch_embeddings.append(embedding)

        embeddings.extend(batch_embeddings)

        # Small delay between batches to be respectful to API
        if i + batch_size < len(texts):
            time.sleep(0.1)

    return embeddings


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
