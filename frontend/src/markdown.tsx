import type { Components } from 'react-markdown'

// Custom renderer that turns [1], [2] etc into citation badges
export function processCitations(text: string): string {
  return text.replace(
    /\[(\d+)\]/g,
    '<span class="citation-badge" data-source="$1">$1</span>'
  )
}

export const markdownComponents: Components = {
  // Override paragraph to process citations
  p: ({ children }) => {
    return <p>{children}</p>
  },
}
