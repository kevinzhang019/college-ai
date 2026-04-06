import { useState, useRef, useEffect, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useStore } from '../store'
import { ask } from '../api'
import type { ChatMessage } from '../types'
import MessageBubble from './MessageBubble'
import LoadingState from './LoadingState'

export default function EssayChatTab() {
  const [input, setInput] = useState('')
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const chatMessages = useStore((s) => s.chatMessages)
  const addChatMessage = useStore((s) => s.addChatMessage)
  const chatLoading = useStore((s) => s.chatLoading)
  const setChatLoading = useStore((s) => s.setChatLoading)
  const college = useStore((s) => s.college)
  const topK = useStore((s) => s.topK)
  const isConnected = useStore((s) => s.isConnected)

  // Scroll to bottom on new messages
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [chatMessages, chatLoading])

  // Auto-resize textarea
  useEffect(() => {
    const ta = textareaRef.current
    if (ta) {
      ta.style.height = 'auto'
      ta.style.height = Math.min(ta.scrollHeight, 120) + 'px'
    }
  }, [input])

  const handleSend = useCallback(async () => {
    const q = input.trim()
    if (!q || chatLoading) return

    const userMsg: ChatMessage = {
      id: Date.now().toString(),
      role: 'user',
      content: q,
      timestamp: Date.now(),
    }
    addChatMessage(userMsg)
    setInput('')
    setChatLoading(true)

    try {
      const result = await ask({
        question: q,
        top_k: topK,
        ...(college ? { college } : {}),
      })
      const assistantMsg: ChatMessage = {
        id: (Date.now() + 1).toString(),
        role: 'assistant',
        content: result.answer,
        sources: result.sources,
        confidence: result.confidence,
        timestamp: Date.now(),
      }
      addChatMessage(assistantMsg)
    } catch {
      const errorMsg: ChatMessage = {
        id: (Date.now() + 1).toString(),
        role: 'assistant',
        content:
          "Sorry, I couldn't process that. Please try again.",
        timestamp: Date.now(),
      }
      addChatMessage(errorMsg)
    } finally {
      setChatLoading(false)
    }
  }, [input, chatLoading, college, topK, addChatMessage, setChatLoading])

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  return (
    <div className="card flex flex-col h-[500px] max-w-4xl mx-auto">
      {/* Messages area */}
      <div className="flex-1 overflow-y-auto custom-scrollbar p-4 space-y-3">
        {chatMessages.length === 0 && (
          <div className="flex flex-col items-center justify-center h-full text-center px-4">
            <span className="text-4xl mb-3">✍️</span>
            <h3 className="text-lg font-medium text-slate-200 mb-1">
              Essay Brainstorm
            </h3>
            <p className="text-sm text-slate-500 max-w-sm">
              Tell me about the essay you're working on. I'll help you brainstorm
              ideas using real college data.
            </p>
            <div className="flex flex-wrap gap-2 mt-4 justify-center">
              {[
                'Help me with my "Why Stanford?" essay',
                'Brainstorm ideas for MIT supplement',
                'What should I write about for Common App?',
              ].map((suggestion) => (
                <button
                  key={suggestion}
                  onClick={() => setInput(suggestion)}
                  className="text-xs bg-navy-800 text-blue-400 px-3 py-1.5 rounded-full border border-navy-700 hover:bg-navy-700 transition-colors"
                >
                  {suggestion}
                </button>
              ))}
            </div>
          </div>
        )}
        {chatMessages.map((msg) => (
          <MessageBubble key={msg.id} message={msg} />
        ))}
        {chatLoading && <LoadingState message="Brainstorming ideas..." />}
        <div ref={messagesEndRef} />
      </div>

      {/* Input area */}
      <div className="border-t border-navy-700 p-3">
        <AnimatePresence mode="wait">
          {!isConnected ? (
            <motion.div
              key="skeleton"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0, scale: 0.98 }}
              transition={{ duration: 0.3 }}
              className="flex gap-2 items-end"
            >
              <div className="flex-1 py-1">
                <div className="h-5 bg-navy-800 rounded-lg animate-pulse w-2/3" />
              </div>
              <div className="shrink-0 w-[40px] h-[32px] bg-navy-800 rounded-full animate-pulse flex items-center justify-center">
                <span className="flex gap-1">
                  <span className="w-1 h-1 bg-slate-600 rounded-full dot-bounce" />
                  <span className="w-1 h-1 bg-slate-600 rounded-full dot-bounce" />
                  <span className="w-1 h-1 bg-slate-600 rounded-full dot-bounce" />
                </span>
              </div>
            </motion.div>
          ) : (
            <motion.div
              key="input"
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
                placeholder="Describe what you need help with..."
                className="flex-1 resize-none bg-transparent text-sm text-slate-100 placeholder:text-slate-500 focus:outline-none leading-relaxed py-1"
                rows={1}
                disabled={chatLoading}
              />
              <button
                onClick={handleSend}
                disabled={!input.trim() || chatLoading}
                className="btn-primary shrink-0 px-3 py-1.5 text-sm"
              >
                <svg
                  className="w-4 h-4"
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M14 5l7 7m0 0l-7 7m7-7H3"
                  />
                </svg>
              </button>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </div>
  )
}
