/**
 * Citation processing utilities for RAG source references.
 *
 * The LLM emits [N] markers that reference numbered sources.
 * These helpers convert them to interactive badge elements or strip them.
 */

/**
 * Convert [N] markers to interactive badge elements, wrapping the preceding
 * sentence in a highlight-able span so hover underlines only that sentence.
 *
 * Consecutive badges like [1][2] are grouped and share the same sentence span,
 * with each source number included in the span's data attribute.
 */
export function processCitations(text: string): string {
  // Match: a sentence (ending at period/!/?) followed by one or more [N] badges,
  // OR a standalone group of [N] badges (e.g. mid-sentence or after a colon).
  return text.replace(
    /([^.!?\n][^.!?\n]*[.!?])\s*(\[(\d+)\](?:\s*\[(\d+)\])*)/g,
    (_match, sentence: string, badgeGroup: string) => {
      // Extract all source numbers from the badge group
      const sourceNums: string[] = []
      badgeGroup.replace(/\[(\d+)\]/g, (_: string, num: string) => {
        sourceNums.push(num)
        return ''
      })

      const dataAttr = sourceNums.join(',')
      const badges = sourceNums
        .map(n => `<span class="source-badge" data-source="${n}">${n}</span>`)
        .join('')

      return `<span class="cite-sentence" data-sources="${dataAttr}">${sentence.trim()}</span>${badges}`
    }
  ).replace(
    // Catch any remaining standalone [N] not preceded by a sentence
    /\[(\d+)\]/g,
    '<span class="source-badge" data-source="$1">$1</span>'
  )
}

/** Remove [N] markers entirely (used when sources are hidden). */
export function stripCitations(text: string): string {
  return text.replace(/\s*\[(\d+)\]/g, '')
}
