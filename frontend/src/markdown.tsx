/**
 * Citation processing utilities for RAG source references.
 *
 * The LLM emits [N] markers that reference numbered sources and
 * [SD] markers for school data from our verified database.
 * These helpers convert them to interactive badge elements or strip them.
 */

const SD_BADGE =
  '<span class="source-badge-official" aria-label="Official Source">' +
  '<svg viewBox="0 0 16 16" fill="none" class="official-check-icon">' +
  '<path d="M13.25 4.75L6 12 2.75 8.75" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>' +
  '</svg>' +
  '<span class="official-tooltip">Official Source</span>' +
  '</span>'

/** Convert [N] markers to interactive <span> badge elements. */
export function processCitations(text: string): string {
  return text
    .replace(/\[SD\]/g, SD_BADGE)
    .replace(
      /\[(\d+)\]/g,
      '<span class="source-badge" data-source="$1">$1</span>'
    )
}

/** Process only [SD] markers (used when numbered sources are hidden). */
export function processOfficialCitations(text: string): string {
  return text
    .replace(/\[SD\]/g, SD_BADGE)
    .replace(/\s*\[(\d+)\]/g, '')
}

/** Remove [N] and [SD] markers entirely (used for plain-text copy). */
export function stripCitations(text: string): string {
  return text.replace(/\s*\[SD\]/g, '').replace(/\s*\[(\d+)\]/g, '')
}

/** Strip citations and common markdown syntax for plain-text copy. */
export function stripMarkdown(text: string): string {
  return stripCitations(text)
    .replace(/#{1,6}\s+/g, '')
    .replace(/\*\*(.+?)\*\*/g, '$1')
    .replace(/\*(.+?)\*/g, '$1')
    .replace(/```[\s\S]*?```/g, (match) => match.replace(/```\w*\n?/g, '').trim())
    .replace(/`(.+?)`/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/^\s*[-*+]\s+/gm, '')
    .replace(/^\s*\d+\.\s+/gm, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}
