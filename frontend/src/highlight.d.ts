/** CSS Custom Highlight API type declarations (not yet in lib "DOM"). */

declare class Highlight {
  constructor(...ranges: Range[])
}

interface HighlightRegistry {
  set(name: string, highlight: Highlight): void
  delete(name: string): boolean
  clear(): void
}

declare namespace CSS {
  const highlights: HighlightRegistry | undefined
}
