import { useState, useRef, useEffect, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useStore } from '../store'
import { useStreaming } from '../hooks/useStreaming'
import CollegeCombobox from './CollegeCombobox'
import ReviewPanel from './ReviewPanel'

export default function InputArea() {
  const [input, setInput] = useState('')
  const [essayText, setEssayText] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const mode = useStore((s) => s.mode)
  const activeConversationId = useStore((s) => s.activeConversationId)
  const conversations = useStore((s) => s.conversations)
  const streamingLoading = useStore((s) => s.streamingLoading)
  const isConnected = useStore((s) => s.isConnected)
  const updateConversationCollege = useStore((s) => s.updateConversationCollege)
  const updateConversationEssayPrompt = useStore((s) => s.updateConversationEssayPrompt)
  const createConversation = useStore((s) => s.createConversation)

  const conversation = activeConversationId
    ? conversations[activeConversationId]
    : null

  const college = conversation?.college || null
  const essayPrompt = conversation?.essayPrompt || ''

  const { send, cancel } = useStreaming()

  // Auto-resize textarea
  useEffect(() => {
    const ta = textareaRef.current
    if (ta) {
      ta.style.height = 'auto'
      ta.style.height = Math.min(ta.scrollHeight, 150) + 'px'
    }
  }, [input])

  const handleCollegeChange = useCallback(
    (value: string | null) => {
      if (activeConversationId) {
        updateConversationCollege(activeConversationId, value)
      } else {
        // Create conversation first, then set college
        const id = createConversation(mode === 'essay' ? 'essay' : 'qa')
        updateConversationCollege(id, value)
      }
    },
    [activeConversationId, mode, updateConversationCollege, createConversation],
  )

  const handleEssayPromptChange = useCallback(
    (value: string) => {
      if (activeConversationId) {
        updateConversationEssayPrompt(activeConversationId, value)
      } else {
        const id = createConversation('essay')
        updateConversationEssayPrompt(id, value)
      }
    },
    [activeConversationId, updateConversationEssayPrompt, createConversation],
  )

  const handleSend = useCallback(async () => {
    const q = input.trim()
    if (!q || streamingLoading) return
    if (mode === 'essay' && !essayPrompt.trim()) return

    setInput('')
    await send(q, essayText || undefined)
  }, [input, streamingLoading, mode, essayPrompt, essayText, send])

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  const canSend =
    input.trim().length > 0 &&
    !streamingLoading &&
    isConnected &&
    (mode !== 'essay' || essayPrompt.trim().length > 0)

  // Full loading skeleton for connecting state
  if (!isConnected) {
    return (
      <div className="border-t border-dark-700 bg-dark-950/80 backdrop-blur-sm">
        <div className="max-w-3xl mx-auto px-4 py-3 space-y-2">
          <AnimatePresence mode="wait">
            <motion.div
              key="connecting"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="space-y-2"
            >
              {/* Skeleton for mode-specific fields */}
              <div className="flex gap-2">
                <div className={mode === 'essay' ? 'w-1/2' : 'w-full'}>
                  <div className="h-9 bg-dark-800 rounded-lg animate-pulse" />
                </div>
                {mode === 'essay' && (
                  <div className="w-1/2">
                    <div className="h-9 bg-dark-800 rounded-lg animate-pulse" />
                  </div>
                )}
              </div>
              {/* Skeleton for chat input */}
              <div className="flex gap-2 items-end">
                <div className="flex-1">
                  <div className="h-10 bg-dark-800 rounded-xl animate-pulse" />
                </div>
                <div className="shrink-0 w-9 h-9 bg-dark-800 rounded-full animate-pulse" />
              </div>
              {/* Connecting label */}
              <div className="flex items-center justify-center gap-2 py-1">
                <span className="flex gap-1">
                  <span className="w-1.5 h-1.5 bg-forest-400 rounded-full dot-bounce" />
                  <span className="w-1.5 h-1.5 bg-forest-400 rounded-full dot-bounce" />
                  <span className="w-1.5 h-1.5 bg-forest-400 rounded-full dot-bounce" />
                </span>
                <span className="text-xs text-slate-500">Connecting to Cole...</span>
              </div>
            </motion.div>
          </AnimatePresence>
        </div>
      </div>
    )
  }

  return (
    <div className="border-t border-dark-700 bg-dark-950/80 backdrop-blur-sm">
      {/* Essay review panel — slides up above input */}
      {mode === 'essay' && (
        <div className="max-w-3xl mx-auto px-4 pt-2">
          <ReviewPanel essayText={essayText} onEssayTextChange={setEssayText} />
        </div>
      )}

      <div className="max-w-3xl mx-auto px-4 py-3 space-y-2">
        {/* Mode-specific fields */}
        <div className="flex gap-2 items-start">
          {/* School selection — shown in both Q&A and Essay */}
          <div className={mode === 'essay' ? 'w-1/2' : 'w-full'}>
            <CollegeCombobox
              value={college}
              onChange={handleCollegeChange}
              compact
            />
          </div>

          {/* Essay prompt field — essay mode only */}
          {mode === 'essay' && (
            <div className="w-1/2">
              <input
                type="text"
                value={essayPrompt}
                onChange={(e) => handleEssayPromptChange(e.target.value)}
                placeholder="Essay prompt (required)"
                className="input-field-compact text-sm"
              />
            </div>
          )}
        </div>

        {/* Chat input */}
        <motion.div
          initial={{ opacity: 0, scale: 0.98 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ type: 'spring', stiffness: 300, damping: 30 }}
          className="flex gap-2 items-end"
        >
          <textarea
            ref={textareaRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={
              mode === 'essay'
                ? 'Tell Cole what to focus on (e.g., "highlight my research experience")...'
                : 'Ask Cole about colleges...'
            }
            className="flex-1 resize-none bg-dark-800 border border-dark-700 rounded-xl px-4 py-2.5 text-sm text-slate-100 placeholder:text-slate-500 focus:outline-none focus:ring-2 focus:ring-forest-500/40 focus:border-forest-500 transition-all leading-relaxed"
            rows={1}
            disabled={streamingLoading}
          />

          {streamingLoading ? (
            <button
              onClick={cancel}
              className="shrink-0 w-9 h-9 flex items-center justify-center rounded-full bg-red-500/20 text-red-400 hover:bg-red-500/30 transition-colors"
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          ) : (
            <button
              onClick={handleSend}
              disabled={!canSend}
              className="shrink-0 w-9 h-9 flex items-center justify-center rounded-full bg-forest-600 text-white hover:bg-forest-500 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M14 5l7 7m0 0l-7 7m7-7H3" />
              </svg>
            </button>
          )}
        </motion.div>
      </div>
    </div>
  )
}
