import { useState, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'

interface Props {
  essayText: string
  onEssayTextChange: (text: string) => void
  essayPrompt: string
  onEssayPromptChange: (prompt: string) => void
  promptWarning?: boolean
  forceOpen?: boolean
}

export default function ReviewPanel({ essayText, onEssayTextChange, essayPrompt, onEssayPromptChange, promptWarning, forceOpen }: Props) {
  const [open, setOpen] = useState(false)

  useEffect(() => {
    if (forceOpen) setOpen(true)
  }, [forceOpen])

  const wordCount = essayText.trim()
    ? essayText.trim().split(/\s+/).length
    : 0

  const hasContent = essayText.trim().length > 0 || essayPrompt.trim().length > 0

  return (
    <>
      {/* Toggle button */}
      <button
        onClick={() => setOpen(!open)}
        className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium border transition-all ${!open && hasContent ? 'bg-forest-500/15 border-forest-500/50 text-forest-400 shadow-[0_0_8px_rgba(34,197,94,0.2)] hover:text-forest-300 hover:border-forest-500/60' : 'bg-dark-800 border-dark-700 text-slate-400 hover:text-slate-200 hover:border-forest-500/40'}`}
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
        {open ? 'Hide Essay' : 'Essay Help'}
      </button>

      {/* Overlay panel — floats above input, does not push content */}
      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 280, opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ type: 'spring', damping: 25, stiffness: 300 }}
            className="absolute bottom-full left-0 right-0 z-40 overflow-hidden"
          >
            <div className="h-full flex flex-col max-w-3xl mx-auto px-4 pb-2 bg-dark-950/95 backdrop-blur-sm border-t border-x border-dark-700 rounded-t-xl">
              {/* Essay prompt input */}
              <div className="pt-2 pb-1.5">
                <input
                  type="text"
                  value={essayPrompt}
                  onChange={(e) => onEssayPromptChange(e.target.value)}
                  placeholder={promptWarning ? "Prompt required when essay is provided" : "Essay prompt (leave blank for general advice)"}
                  className={`input-field-compact text-sm w-full ${promptWarning ? 'border-red-500/60 ring-1 ring-red-500/30 placeholder:text-red-400/70' : ''}`}
                />
              </div>

              <div className="flex items-center justify-between py-1.5">
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
                className="flex-1 px-3 py-2 bg-dark-800 resize-none text-sm text-slate-200 leading-relaxed placeholder:text-slate-500 focus:outline-none focus:ring-2 focus:ring-forest-500/40 focus:border-forest-500 border border-dark-700 rounded-lg mb-2 transition-all duration-200"
              />
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </>
  )
}
