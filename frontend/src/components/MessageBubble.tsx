import { useState, useMemo, useEffect, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeRaw from 'rehype-raw'
import type { ChatMessage } from '../types'
import { processCitations, stripCitations } from '../markdown'
import ConfidenceBadge from './ConfidenceBadge'
import SourceCard from './SourceCard'

/**
 * Walk backwards from a source badge to find the preceding sentence text,
 * returning a Range suitable for the CSS Custom Highlight API.
 */
function getSentenceRange(badge: Element): Range | null {
  // Walk past consecutive sibling badges (for [2][3] groups)
  let node: ChildNode = badge
  while (
    node.previousSibling &&
    node.previousSibling.nodeType === Node.ELEMENT_NODE &&
    (node.previousSibling as Element).classList?.contains('source-badge')
  ) {
    node = node.previousSibling
  }

  const prev = node.previousSibling
  if (!prev) return null

  // Handle text node: skip leading punctuation/space from previous sentence
  if (prev.nodeType === Node.TEXT_NODE) {
    const text = prev.textContent || ''
    let start = 0
    while (start < text.length && '.!? \n\r'.includes(text[start])) start++
    let end = text.length
    while (end > start && ' \n\r'.includes(text[end - 1])) end--
    if (start >= end) return null

    const range = document.createRange()
    range.setStart(prev, start)
    range.setEnd(prev, end)
    return range
  }

  // Handle element node (e.g. <strong>, <em>): highlight its contents
  if (prev.nodeType === Node.ELEMENT_NODE) {
    const range = document.createRange()
    range.selectNodeContents(prev)
    return range
  }

  return null
}

export default function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === 'user'
  const [showSources, setShowSources] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)

  const hasSources = Boolean(message.sources?.length)

  const processedContent = useMemo(() => {
    if (!hasSources) return message.content
    return showSources
      ? processCitations(message.content)
      : stripCitations(message.content)
  }, [message.content, showSources, hasSources])

  // Event delegation for citation badge hover + click
  useEffect(() => {
    const container = containerRef.current
    if (!container || !showSources) return

    const handleMouseOver = (e: MouseEvent) => {
      const badge = (e.target as HTMLElement).closest('.source-badge') as HTMLElement | null
      if (!badge) return

      badge.classList.add('source-badge--active')

      const range = getSentenceRange(badge)
      if (range && CSS.highlights) {
        CSS.highlights.set('source-hl', new Highlight(range))
      } else if (range) {
        // Fallback: highlight the parent block
        const block = badge.closest('p, li, blockquote')
        if (block) block.classList.add('source-highlight')
      }
    }

    const handleMouseOut = (e: MouseEvent) => {
      const badge = (e.target as HTMLElement).closest('.source-badge') as HTMLElement | null
      if (!badge) return

      badge.classList.remove('source-badge--active')

      if (CSS.highlights) {
        CSS.highlights.delete('source-hl')
      } else {
        const block = badge.closest('p, li, blockquote')
        if (block) block.classList.remove('source-highlight')
      }
    }

    const handleClick = (e: MouseEvent) => {
      const badge = (e.target as HTMLElement).closest('.source-badge') as HTMLElement | null
      if (!badge) return
      const sourceNum = badge.dataset.source
      if (!sourceNum) return

      const cardIndex = parseInt(sourceNum, 10) - 1
      const card = container.querySelector(`#source-${cardIndex}`)
      if (card) {
        card.scrollIntoView({ block: 'center', behavior: 'smooth' })
        card.classList.add('ring-2', 'ring-forest-400/50')
        setTimeout(() => card.classList.remove('ring-2', 'ring-forest-400/50'), 1500)
      }
    }

    container.addEventListener('mouseover', handleMouseOver)
    container.addEventListener('mouseout', handleMouseOut)
    container.addEventListener('click', handleClick)

    return () => {
      container.removeEventListener('mouseover', handleMouseOver)
      container.removeEventListener('mouseout', handleMouseOut)
      container.removeEventListener('click', handleClick)
    }
  }, [showSources])

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className={`flex gap-3 ${isUser ? 'justify-end' : 'justify-start'}`}
    >
      {isUser ? (
        <div className="max-w-[85%] bg-forest-600 text-white rounded-2xl rounded-br-md px-4 py-3">
          <p className="text-sm leading-relaxed">{message.content}</p>
        </div>
      ) : (
        <div ref={containerRef} className="w-full py-1">
          <div className="flex items-center gap-2 mb-1.5">
            <div className="w-5 h-5 rounded-full bg-forest-600 flex items-center justify-center text-white text-[10px] font-bold shadow-dark-sm shrink-0">C</div>
            <span className="text-sm font-semibold text-forest-400">Cole</span>
            {message.confidence && (
              <ConfidenceBadge confidence={message.confidence} />
            )}
            {hasSources && (
              <button
                onClick={() => setShowSources(prev => !prev)}
                className="ml-auto text-xs font-medium px-2.5 py-1 rounded-full
                           bg-forest-500/15 text-forest-400 border border-forest-500/30
                           hover:bg-forest-500/25 transition-colors"
              >
                {showSources ? 'Hide Sources' : 'Show Sources'}
              </button>
            )}
          </div>
          <div className="markdown-answer text-sm text-slate-300">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              rehypePlugins={[rehypeRaw]}
            >
              {processedContent}
            </ReactMarkdown>
          </div>
          <AnimatePresence>
            {showSources && message.sources && message.sources.length > 0 && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ duration: 0.25, ease: 'easeInOut' }}
                className="mt-3 space-y-1.5 overflow-hidden"
              >
                <p className="text-xs text-slate-500 font-medium">Sources</p>
                {message.sources.map((source, i) => (
                  <SourceCard key={source.url + i} source={source} index={i} />
                ))}
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      )}
    </motion.div>
  )
}
