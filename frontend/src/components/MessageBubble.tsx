import { useState, useMemo, useEffect, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeRaw from 'rehype-raw'
import type { ChatMessage } from '../types'
import { processCitations, processOfficialCitations, stripMarkdown } from '../markdown'
import { useStore } from '../store'
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

function CopyIcon({ className }: { className?: string }) {
  return (
    <svg className={className || "w-3.5 h-3.5"} fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
    </svg>
  )
}

function CheckIcon({ className }: { className?: string }) {
  return (
    <svg className={className || "w-3.5 h-3.5"} fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" />
    </svg>
  )
}

function EditIcon({ className }: { className?: string }) {
  return (
    <svg className={className || "w-3.5 h-3.5"} fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
    </svg>
  )
}

function ActionButton({ onClick, title, children }: { onClick: () => void; title: string; children: React.ReactNode }) {
  return (
    <button
      onClick={onClick}
      title={title}
      className="w-7 h-7 flex items-center justify-center rounded-md bg-dark-800/80 border border-dark-700 text-slate-400 hover:text-slate-200 hover:border-slate-500 transition-colors"
    >
      {children}
    </button>
  )
}

export default function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === 'user'
  const [showSources, setShowSources] = useState(false)
  const [copied, setCopied] = useState(false)
  const [showDraft, setShowDraft] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)

  const hasSources = Boolean(message.sources?.length)

  const hasOfficialCitations = message.content.includes('[SD]')

  const processedContent = useMemo(() => {
    if (!hasSources && !hasOfficialCitations) return message.content
    if (showSources) return processCitations(message.content)
    // Always show [SD] badges even when numbered sources are hidden
    return processOfficialCitations(message.content)
  }, [message.content, showSources, hasSources, hasOfficialCitations])

  const handleCopy = () => {
    let text: string
    if (isUser) {
      text = message.content
    } else {
      text = stripMarkdown(message.content)
    }
    navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  const handleEdit = () => {
    useStore.getState().setPendingEdit(message)
  }

  // Event delegation for citation badge hover + click
  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const handleMouseOver = (e: MouseEvent) => {
      const el = e.target as HTMLElement
      const badge = el.closest('.source-badge, .source-badge-official') as HTMLElement | null
      if (!badge) return

      if (badge.classList.contains('source-badge')) {
        badge.classList.add('source-badge--active')
      } else {
        badge.classList.add('source-badge-official--active')
      }

      const range = getSentenceRange(badge)
      if (range && CSS.highlights) {
        CSS.highlights.set('source-hl', new Highlight(range))
      } else if (range) {
        const block = badge.closest('p, li, blockquote')
        if (block) block.classList.add('source-highlight')
      }
    }

    const handleMouseOut = (e: MouseEvent) => {
      const el = e.target as HTMLElement
      const badge = el.closest('.source-badge, .source-badge-official') as HTMLElement | null
      if (!badge) return

      badge.classList.remove('source-badge--active', 'source-badge-official--active')

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
  }, [showSources, hasOfficialCitations])

  return (
    <>
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className={`group flex gap-3 ${isUser ? 'justify-end' : 'justify-start'}`}
    >
      {isUser ? (
        <div className="flex flex-col items-end max-w-[85%] ml-auto">
          <div className="bg-forest-600 text-white rounded-2xl rounded-br-md px-4 py-3 w-full">
            {message.essayPrompt && (
              <p className="text-[13px] font-semibold leading-snug mb-1.5 opacity-90">
                {message.essayPrompt}
              </p>
            )}
            <div className="flex items-start gap-2">
              <p className="text-sm leading-relaxed flex-1">{message.content}</p>
              {message.hasEssayDraft && (
                <button
                  onClick={() => setShowDraft(true)}
                  title="Display full essay draft"
                  className="group/draft shrink-0 mt-0.5 inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium bg-white/15 text-white/80 backdrop-blur-sm border border-white/10 hover:bg-forest-500/30 hover:text-white hover:border-forest-500/40 cursor-pointer transition-all duration-200"
                >
                  <svg className="w-2.5 h-2.5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                  </svg>
                  <span className="inline overflow-hidden whitespace-nowrap max-w-[5.5rem] group-hover/draft:max-w-0 transition-all duration-200">Draft loaded</span>
                  <span className="inline overflow-hidden whitespace-nowrap max-w-0 group-hover/draft:max-w-[10rem] transition-all duration-200">Display full essay draft</span>
                </button>
              )}
            </div>
          </div>
          {/* Action buttons — below user bubble, right-aligned */}
          <div className="flex gap-1 mt-1 opacity-0 group-hover:opacity-100 transition-opacity duration-150">
            <ActionButton onClick={handleEdit} title="Edit">
              <EditIcon />
            </ActionButton>
            <ActionButton onClick={handleCopy} title={copied ? 'Copied!' : 'Copy'}>
              {copied ? <CheckIcon className="w-3.5 h-3.5 text-forest-400" /> : <CopyIcon />}
            </ActionButton>
          </div>
        </div>
      ) : (
        <div ref={containerRef} className="group/msg w-full py-1">
          <div className="flex items-center gap-2 mb-1.5">
            <img src="/cole-logo.png" alt="Cole" className="h-5 w-auto shrink-0" />
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
          {/* Copy button — below response, left-aligned */}
          <div className="flex gap-1 mt-1 opacity-0 group-hover/msg:opacity-100 transition-opacity duration-150">
            <ActionButton onClick={handleCopy} title={copied ? 'Copied!' : 'Copy'}>
              {copied ? <CheckIcon className="w-3.5 h-3.5 text-forest-400" /> : <CopyIcon />}
            </ActionButton>
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

    {/* Essay draft modal */}
    {showDraft && message.essayText && (
      <div className="fixed inset-0 z-50 flex items-center justify-center" onClick={() => setShowDraft(false)}>
        <div className="absolute inset-0 bg-black/60" />
        <div
          className="relative w-full max-w-2xl max-h-[70vh] mx-4 bg-dark-900 border border-dark-700 rounded-2xl shadow-dark-lg flex flex-col"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="flex items-center justify-between px-5 py-3.5 border-b border-dark-700">
            <h3 className="text-sm font-semibold text-slate-200">Essay Draft</h3>
            <button
              onClick={() => setShowDraft(false)}
              className="w-7 h-7 flex items-center justify-center rounded-md text-slate-400 hover:text-slate-200 hover:bg-dark-800 transition-colors"
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
          <div className="flex-1 overflow-y-auto custom-scrollbar px-5 py-4">
            <p className="text-sm text-slate-300 leading-relaxed whitespace-pre-wrap">{message.essayText}</p>
          </div>
        </div>
      </div>
    )}
    </>
  )
}
