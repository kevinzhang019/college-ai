import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'

interface Props {
  essayText: string
  onEssayTextChange: (text: string) => void
}

export default function ReviewPanel({ essayText, onEssayTextChange }: Props) {
  const [open, setOpen] = useState(false)

  const wordCount = essayText.trim()
    ? essayText.trim().split(/\s+/).length
    : 0

  return (
    <>
      {/* Toggle button */}
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium bg-dark-800 border border-dark-700 text-slate-400 hover:text-slate-200 hover:border-forest-500/40 transition-all"
      >
        <motion.svg
          animate={{ rotate: open ? 180 : 0 }}
          transition={{ duration: 0.2 }}
          className="w-3.5 h-3.5"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 15l7-7 7 7" />
        </motion.svg>
        {open ? 'Hide Draft' : 'Review Draft'}
      </button>

      {/* Slide-up panel */}
      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 220, opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ type: 'spring', damping: 25, stiffness: 300 }}
            className="overflow-hidden border-t border-dark-700"
          >
            <div className="h-full flex flex-col">
              <div className="flex items-center justify-between px-4 py-1.5 bg-dark-900/50">
                <span className="text-xs font-medium text-slate-400">
                  Your Essay Draft
                </span>
                <span className="text-xs text-slate-600">
                  {wordCount} {wordCount === 1 ? 'word' : 'words'}
                </span>
              </div>
              <textarea
                value={essayText}
                onChange={(e) => onEssayTextChange(e.target.value)}
                placeholder="Paste your essay draft here for review feedback..."
                className="flex-1 px-4 py-2 bg-transparent resize-none text-sm text-slate-200 leading-relaxed placeholder:text-slate-600 focus:outline-none"
              />
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </>
  )
}
