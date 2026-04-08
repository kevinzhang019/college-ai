import { useState, useRef, useEffect, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Popover, PopoverButton, PopoverPanel } from '@headlessui/react'
import { useStore } from '../store'
import { useStreaming } from '../hooks/useStreaming'
import CollegeCombobox from './CollegeCombobox'
import ReviewPanel from './ReviewPanel'
import QuickPredictModal from './QuickPredictModal'
import type { ContextSize, ResponseLength } from '../types'

const SIZE_OPTIONS: ContextSize[] = ['XS', 'S', 'M', 'L', 'XL']

export default function InputArea() {
  const [input, setInput] = useState('')
  const [essayText, setEssayText] = useState('')
  const [promptWarning, setPromptWarning] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const activeConversationId = useStore((s) => s.activeConversationId)
  const conversations = useStore((s) => s.conversations)
  const streamingLoading = useStore((s) => s.streamingLoading)
  const isConnected = useStore((s) => s.isConnected)
  const updateConversationCollege = useStore((s) => s.updateConversationCollege)
  const updateConversationEssayPrompt = useStore((s) => s.updateConversationEssayPrompt)
  const createConversation = useStore((s) => s.createConversation)
  const contextSize = useStore((s) => s.contextSize)
  const setContextSize = useStore((s) => s.setContextSize)
  const responseLength = useStore((s) => s.responseLength)
  const setResponseLength = useStore((s) => s.setResponseLength)

  const conversation = activeConversationId
    ? conversations[activeConversationId]
    : null

  const college = conversation?.college || null
  const essayPrompt = conversation?.essayPrompt || ''

  const [quickPredictOpen, setQuickPredictOpen] = useState(false)
  const chancesContainerRef = useRef<HTMLDivElement>(null)

  const { send, cancel } = useStreaming()

  // Auto-resize textarea
  useEffect(() => {
    const ta = textareaRef.current
    if (ta) {
      ta.style.height = 'auto'
      ta.style.height = Math.min(ta.scrollHeight, 150) + 'px'
    }
  }, [input])

  // Click-outside to close chances popup
  useEffect(() => {
    if (!quickPredictOpen) return
    const handler = (e: MouseEvent) => {
      if (chancesContainerRef.current && !chancesContainerRef.current.contains(e.target as Node)) {
        setQuickPredictOpen(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [quickPredictOpen])

  const handleCollegeChange = useCallback(
    (value: string | null) => {
      if (activeConversationId) {
        updateConversationCollege(activeConversationId, value)
      } else {
        const id = createConversation('qa')
        updateConversationCollege(id, value)
      }
    },
    [activeConversationId, updateConversationCollege, createConversation],
  )

  const handleEssayPromptChange = useCallback(
    (value: string) => {
      if (activeConversationId) {
        updateConversationEssayPrompt(activeConversationId, value)
      } else {
        const id = createConversation('qa')
        updateConversationEssayPrompt(id, value)
      }
      if (value.trim()) setPromptWarning(false)
    },
    [activeConversationId, updateConversationEssayPrompt, createConversation],
  )

  const handleSend = useCallback(async () => {
    const q = input.trim()
    if (!q || streamingLoading) return

    // Validate: essay text requires a prompt
    if (essayText.trim() && !essayPrompt.trim()) {
      setPromptWarning(true)
      return
    }

    setInput('')
    setPromptWarning(false)
    await send(q, essayText.trim() || undefined)
  }, [input, streamingLoading, essayText, essayPrompt, send])

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  const canSend =
    input.trim().length > 0 &&
    !streamingLoading &&
    isConnected

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
              {/* Skeleton for college field */}
              <div className="flex gap-2">
                <div className="w-full">
                  <div className="h-9 bg-dark-800 rounded-lg animate-pulse" />
                </div>
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
      <div className="max-w-3xl mx-auto px-4 pt-2">
        <ReviewPanel
          essayText={essayText}
          onEssayTextChange={setEssayText}
          essayPrompt={essayPrompt}
          onEssayPromptChange={handleEssayPromptChange}
          promptWarning={promptWarning}
        />
      </div>

      <div className="max-w-3xl mx-auto px-4 py-3 space-y-2">
        {/* School selection + info/chances */}
        <div className="flex gap-2 items-start">
          <div className="flex-1">
            <CollegeCombobox
              value={college}
              onChange={handleCollegeChange}
              compact
            />
          </div>

          {/* Info tooltip — shown when no college selected */}
          {!college && (
            <div className="relative shrink-0 group">
              <div className="flex items-center justify-center w-8 h-8 rounded-full border border-dark-700 text-slate-500 hover:text-slate-300 hover:border-slate-500 transition-colors cursor-help">
                <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
              </div>
              <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 w-48 px-3 py-2 bg-dark-800 border border-dark-700 rounded-lg shadow-xl text-xs text-slate-300 leading-relaxed opacity-0 pointer-events-none group-hover:opacity-100 transition-opacity z-50">
                Or just mention the school in your question, Cole will understand.
              </div>
            </div>
          )}

          {/* Chances button — inline with school dropdown */}
          {college && (
            <div className="relative shrink-0 self-end" ref={chancesContainerRef}>
              <AnimatePresence>
                {quickPredictOpen && (
                  <div className="absolute bottom-full right-0 mb-2 z-50 min-w-[480px]">
                    <QuickPredictModal college={college} />
                  </div>
                )}
              </AnimatePresence>
              <button
                onClick={() => setQuickPredictOpen(!quickPredictOpen)}
                className="group flex flex-col items-center gap-0 px-2 rounded-md text-forest-400 bg-transparent transition-all cursor-pointer text-[11px] font-medium"
                title="Estimate admission chances"
              >
                <svg className="w-3 h-3 transition-transform duration-200 group-hover:-translate-y-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 15l7-7 7 7" />
                </svg>
                <span>My chances</span>
              </button>
            </div>
          )}

          {/* Spacer to align with send button below */}
          <div className="shrink-0 w-9" />

        </div>

        {/* Chat input */}
        <div className="flex gap-2 items-end">
          <div className="flex-1">
            <motion.div
              initial={{ opacity: 0, scale: 0.98 }}
              animate={{ opacity: 1, scale: 1 }}
              transition={{ type: 'spring', stiffness: 300, damping: 30 }}
              className="relative"
            >
            <textarea
              ref={textareaRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Ask Cole about colleges..."
              className="w-full resize-none bg-dark-800 border border-dark-700 rounded-xl px-4 py-2.5 pb-8 text-sm text-slate-100 placeholder:text-slate-500 focus:outline-none focus:ring-2 focus:ring-forest-500/40 focus:border-forest-500 transition-all leading-relaxed"
              rows={1}
              disabled={streamingLoading}
            />

              {/* Settings dropdown — bottom-right of textarea */}
              <div className="absolute bottom-1.5 right-2">
                <Popover className="relative">
                  <PopoverButton className="flex items-center gap-1 px-1.5 py-0.5 rounded text-xs text-slate-400 hover:text-slate-200 hover:bg-dark-700 transition-colors">
                    <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.75} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.75} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                    </svg>
                    <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                    </svg>
                  </PopoverButton>
                  <PopoverPanel className="absolute bottom-full right-0 mb-1 w-48 bg-dark-800 border border-dark-700 rounded-lg shadow-xl z-50 py-2">
                    {/* Context Size */}
                    <div className="px-3 pb-1.5">
                      <span className="text-[10px] font-medium text-slate-500 uppercase tracking-wider">Context Size</span>
                    </div>
                    <div className="flex gap-1 px-3 pb-2.5">
                      {SIZE_OPTIONS.map((size) => (
                        <button
                          key={`ctx-${size}`}
                          onClick={() => setContextSize(size)}
                          className={`flex-1 py-1 rounded text-xs font-medium transition-colors ${
                            contextSize === size
                              ? 'bg-forest-600/25 text-forest-400 border border-forest-500/30'
                              : 'text-slate-400 hover:text-slate-200 hover:bg-dark-700 border border-transparent'
                          }`}
                        >
                          {size}
                        </button>
                      ))}
                    </div>

                    <div className="border-t border-dark-700 my-1" />

                    {/* Response Length */}
                    <div className="px-3 pb-1.5 pt-1">
                      <span className="text-[10px] font-medium text-slate-500 uppercase tracking-wider">Response Length</span>
                    </div>
                    <div className="flex gap-1 px-3 pb-1">
                      {SIZE_OPTIONS.map((size) => (
                        <button
                          key={`len-${size}`}
                          onClick={() => setResponseLength(size as ResponseLength)}
                          className={`flex-1 py-1 rounded text-xs font-medium transition-colors ${
                            responseLength === size
                              ? 'bg-forest-600/25 text-forest-400 border border-forest-500/30'
                              : 'text-slate-400 hover:text-slate-200 hover:bg-dark-700 border border-transparent'
                          }`}
                        >
                          {size}
                        </button>
                      ))}
                    </div>
                  </PopoverPanel>
                </Popover>
              </div>
            </motion.div>
          </div>

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
        </div>
      </div>
    </div>
  )
}
