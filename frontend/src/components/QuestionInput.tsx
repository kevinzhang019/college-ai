import { useRef, useEffect, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useStore } from '../store'
import { ask } from '../api'

function InputSkeleton() {
  return (
    <motion.div
      key="skeleton"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0, scale: 0.98 }}
      transition={{ duration: 0.3 }}
      className="card p-4"
    >
      <div className="flex gap-3 items-end">
        <div className="flex-1 py-1">
          <div className="h-5 bg-navy-800 rounded-lg animate-pulse w-3/4" />
        </div>
        <div className="shrink-0 w-[52px] h-[36px] bg-navy-800 rounded-full animate-pulse flex items-center justify-center">
          <span className="flex gap-1">
            <span className="w-1.5 h-1.5 bg-slate-600 rounded-full dot-bounce" />
            <span className="w-1.5 h-1.5 bg-slate-600 rounded-full dot-bounce" />
            <span className="w-1.5 h-1.5 bg-slate-600 rounded-full dot-bounce" />
          </span>
        </div>
      </div>
    </motion.div>
  )
}

export default function QuestionInput() {
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const qaQuestion = useStore((s) => s.qaQuestion)
  const setQaQuestion = useStore((s) => s.setQaQuestion)
  const qaLoading = useStore((s) => s.qaLoading)
  const setQaLoading = useStore((s) => s.setQaLoading)
  const setQaResult = useStore((s) => s.setQaResult)
  const college = useStore((s) => s.college)
  const topK = useStore((s) => s.topK)
  const isConnected = useStore((s) => s.isConnected)

  // Auto-resize textarea
  useEffect(() => {
    const ta = textareaRef.current
    if (ta) {
      ta.style.height = 'auto'
      ta.style.height = Math.min(ta.scrollHeight, 150) + 'px'
    }
  }, [qaQuestion])

  const handleSubmit = useCallback(async () => {
    const q = qaQuestion.trim()
    if (!q || qaLoading) return
    setQaLoading(true)
    setQaResult(null)
    try {
      const result = await ask({
        question: q,
        top_k: topK,
        ...(college ? { college } : {}),
      })
      setQaResult(result)
    } catch (err) {
      console.error('Ask failed:', err)
      setQaResult({
        answer: 'Sorry, something went wrong. Please try again.',
        sources: [],
        confidence: 'low',
        source_count: 0,
        query_type: 'qa',
      })
    } finally {
      setQaLoading(false)
    }
  }, [qaQuestion, qaLoading, college, topK, setQaLoading, setQaResult])

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSubmit()
    }
  }

  return (
    <AnimatePresence mode="wait">
      {!isConnected ? (
        <InputSkeleton />
      ) : (
        <motion.div
          key="input"
          initial={{ opacity: 0, scale: 0.98 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ type: 'spring', stiffness: 300, damping: 30 }}
          className="card p-4"
        >
          <div className="flex gap-3 items-end">
            <textarea
              ref={textareaRef}
              value={qaQuestion}
              onChange={(e) => setQaQuestion(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Ask about admissions, requirements, deadlines, scholarships..."
              className="flex-1 resize-none bg-transparent text-slate-100 placeholder:text-slate-500 focus:outline-none text-sm leading-relaxed py-1"
              rows={1}
              disabled={qaLoading}
            />
            <button
              onClick={handleSubmit}
              disabled={!qaQuestion.trim() || qaLoading}
              className="btn-primary shrink-0 px-4 py-2 text-sm"
            >
              {qaLoading ? (
                <span className="flex gap-1">
                  <span className="w-1.5 h-1.5 bg-white rounded-full dot-bounce" />
                  <span className="w-1.5 h-1.5 bg-white rounded-full dot-bounce" />
                  <span className="w-1.5 h-1.5 bg-white rounded-full dot-bounce" />
                </span>
              ) : (
                <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M14 5l7 7m0 0l-7 7m7-7H3" />
                </svg>
              )}
            </button>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
