/**
 * Citation processing utilities for RAG source references.
 *
 * The LLM emits [N] markers that reference numbered sources.
 * These helpers convert them to interactive badge elements or strip them.
 */

/** Convert [N] markers to interactive <span> badge elements. */
export function processCitations(text: string): string {
  return text.replace(
    /\[(\d+)\]/g,
    '<span class="source-badge" data-source="$1">$1</span>'
  )
}

/** Remove [N] markers entirely (used when sources are hidden). */
export function stripCitations(text: string): string {
  return text.replace(/\s*\[(\d+)\]/g, '')
}
