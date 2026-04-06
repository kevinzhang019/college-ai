import { useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useStore } from '../store'
import { ask } from '../api'
import FeedbackPanel from './FeedbackPanel'
import LoadingState from './LoadingState'

export default function EssayEditorTab() {
  const essayText = useStore((s) => s.essayText)
  const setEssayText = useStore((s) => s.setEssayText)
  const editorFeedback = useStore((s) => s.editorFeedback)
  const setEditorFeedback = useStore((s) => s.setEditorFeedback)
  const editorLoading = useStore((s) => s.editorLoading)
  const setEditorLoading = useStore((s) => s.setEditorLoading)
  const college = useStore((s) => s.college)
  const topK = useStore((s) => s.topK)

  const handleGetFeedback = useCallback(async () => {
    if (!essayText.trim() || editorLoading) return
    setEditorLoading(true)
    setEditorFeedback(null)
    try {
      const result = await ask({
        question: 'Review my essay and give detailed feedback',
        essay_text: essayText,
        top_k: topK,
        ...(college ? { college } : {}),
      })
      setEditorFeedback(result)
    } catch {
      setEditorFeedback({
        answer: 'Sorry, something went wrong. Please try again.',
        sources: [],
        confidence: 'low',
        source_count: 0,
        query_type: 'essay_review',
      })
    } finally {
      setEditorLoading(false)
    }
  }, [essayText, editorLoading, college, topK, setEditorLoading, setEditorFeedback])

  const handleGetIdeas = useCallback(async () => {
    if (editorLoading) return
    setEditorLoading(true)
    setEditorFeedback(null)
    try {
      const prompt = college
        ? `Help me brainstorm essay ideas for ${college}`
        : 'Help me brainstorm college essay ideas'
      const result = await ask({
        question: prompt,
        top_k: topK,
        ...(college ? { college } : {}),
      })
      setEditorFeedback(result)
    } catch {
      setEditorFeedback({
        answer: 'Sorry, something went wrong. Please try again.',
        sources: [],
        confidence: 'low',
        source_count: 0,
        query_type: 'essay_ideas',
      })
    } finally {
      setEditorLoading(false)
    }
  }, [editorLoading, college, topK, setEditorLoading, setEditorFeedback])

  const wordCount = essayText.trim()
    ? essayText.trim().split(/\s+/).length
    : 0

  return (
    <div className="max-w-5xl mx-auto">
      {/* Action buttons */}
      <div className="flex gap-2 mb-3 justify-center">
        <button
          onClick={handleGetIdeas}
          disabled={editorLoading}
          className="btn-secondary text-sm"
        >
          💡 Get Ideas
        </button>
        <button
          onClick={handleGetFeedback}
          disabled={!essayText.trim() || editorLoading}
          className="btn-primary text-sm"
        >
          📝 Get Feedback
        </button>
      </div>

      {/* Split panel */}
      <div className="flex flex-col md:flex-row gap-4 min-h-[400px]">
        {/* Left — editor */}
        <div className="flex-1 card flex flex-col">
          <div className="flex items-center justify-between px-4 py-2 border-b border-navy-700">
            <span className="text-xs font-medium text-slate-400">
              Your Essay
            </span>
            <span className="text-xs text-slate-500">
              {wordCount} {wordCount === 1 ? 'word' : 'words'}
            </span>
          </div>
          <textarea
            value={essayText}
            onChange={(e) => setEssayText(e.target.value)}
            placeholder="Paste your essay draft here, or start writing..."
            className="flex-1 p-4 bg-transparent resize-none focus:outline-none text-sm text-slate-200 leading-relaxed placeholder:text-slate-500"
          />
        </div>

        {/* Right — feedback */}
        <div className="flex-1 card flex flex-col overflow-hidden">
          <div className="px-4 py-2 border-b border-navy-700">
            <span className="text-xs font-medium text-slate-400">
              AI Feedback
            </span>
          </div>
          <AnimatePresence mode="wait">
            {editorLoading ? (
              <motion.div
                key="loading"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                className="flex-1 flex items-center justify-center"
              >
                <LoadingState message="Reviewing your essay..." />
              </motion.div>
            ) : editorFeedback ? (
              <motion.div
                key="feedback"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                className="flex-1 overflow-hidden"
              >
                <FeedbackPanel feedback={editorFeedback} />
              </motion.div>
            ) : (
              <motion.div
                key="empty"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                className="flex-1 flex flex-col items-center justify-center text-center px-6"
              >
                <span className="text-3xl mb-2">📋</span>
                <p className="text-sm text-slate-500">
                  Paste your essay and click "Get Feedback" for AI-powered
                  review, or click "Get Ideas" to brainstorm.
                </p>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </div>
    </div>
  )
}
