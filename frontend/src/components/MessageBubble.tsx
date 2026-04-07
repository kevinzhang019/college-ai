import { useState, useMemo, useEffect, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeRaw from 'rehype-raw'
import type { ChatMessage } from '../types'
import { processCitations, stripCitations } from '../markdown'
import ConfidenceBadge from './ConfidenceBadge'
import SourceCard from './SourceCard'

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
      const sourceNum = badge.dataset.source
      if (!sourceNum) return

      // Highlight only this specific badge
      badge.classList.add('source-badge--active')

      // Walk back past any sibling badges to find the cite-sentence span
      let prev = badge.previousElementSibling
      while (prev && prev.classList.contains('source-badge')) {
        prev = prev.previousElementSibling
      }
      if (prev?.classList.contains('cite-sentence') &&
          prev.getAttribute('data-sources')?.split(',').includes(sourceNum)) {
        prev.classList.add('source-highlight')
      }
    }

    const handleMouseOut = (e: MouseEvent) => {
      const badge = (e.target as HTMLElement).closest('.source-badge') as HTMLElement | null
      if (!badge) return

      badge.classList.remove('source-badge--active')

      let prev = badge.previousElementSibling
      while (prev && prev.classList.contains('source-badge')) {
        prev = prev.previousElementSibling
      }
      if (prev?.classList.contains('cite-sentence')) {
        prev.classList.remove('source-highlight')
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
