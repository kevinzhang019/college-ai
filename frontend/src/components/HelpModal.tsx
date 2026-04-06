import { motion, AnimatePresence } from 'framer-motion'
import { useStore } from '../store'

const categories = [
  {
    title: 'Admissions & Requirements',
    icon: '🎯',
    questions: [
      'What is the acceptance rate at Stanford?',
      'What SAT scores does MIT look for?',
      'What are the application deadlines for Ivy League schools?',
      'Does Cornell require SAT subject tests?',
    ],
  },
  {
    title: 'Scholarships & Aid',
    icon: '💰',
    questions: [
      'What scholarships does Rice University offer?',
      'Does Harvard meet 100% of demonstrated need?',
      'What is the average financial aid package at Duke?',
    ],
  },
  {
    title: 'Essay Help',
    icon: '✍️',
    questions: [
      'Help me brainstorm ideas for my Why Stanford essay',
      'What should I write about for the MIT supplement?',
      'Review my Common App personal statement',
    ],
  },
]

export default function HelpModal() {
  const helpOpen = useStore((s) => s.helpOpen)
  const setHelpOpen = useStore((s) => s.setHelpOpen)
  const setMode = useStore((s) => s.setMode)
  const setQaQuestion = useStore((s) => s.setQaQuestion)

  const handleSelect = (question: string) => {
    setMode('qa')
    setQaQuestion(question)
    setHelpOpen(false)
  }

  return (
    <AnimatePresence>
      {helpOpen && (
        <>
          {/* Backdrop */}
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 bg-black/60 backdrop-blur-sm z-40"
            onClick={() => setHelpOpen(false)}
          />
          {/* Modal */}
          <motion.div
            initial={{ opacity: 0, scale: 0.95, y: 20 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.95, y: 20 }}
            transition={{ type: 'spring', bounce: 0.2, duration: 0.4 }}
            className="fixed inset-4 sm:inset-auto sm:top-1/2 sm:left-1/2 sm:-translate-x-1/2 sm:-translate-y-1/2 sm:w-full sm:max-w-lg sm:max-h-[80vh] bg-navy-900 rounded-2xl shadow-dark-lg border border-navy-700 z-50 overflow-hidden flex flex-col"
          >
            {/* Header */}
            <div className="flex items-center justify-between px-6 py-4 border-b border-navy-700">
              <div className="flex items-center gap-2">
                <span className="text-xl">💡</span>
                <h2 className="font-semibold text-slate-100">
                  Example Questions
                </h2>
              </div>
              <button
                onClick={() => setHelpOpen(false)}
                className="p-1 text-slate-500 hover:text-slate-300 transition-colors"
              >
                <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
            {/* Content */}
            <div className="overflow-y-auto custom-scrollbar p-6 space-y-5">
              {categories.map((cat) => (
                <div key={cat.title}>
                  <h3 className="text-sm font-medium text-slate-300 flex items-center gap-2 mb-2">
                    <span>{cat.icon}</span>
                    {cat.title}
                  </h3>
                  <div className="space-y-1.5">
                    {cat.questions.map((q) => (
                      <button
                        key={q}
                        onClick={() => handleSelect(q)}
                        className="w-full text-left text-sm text-slate-400 hover:text-blue-400 hover:bg-navy-800 px-3 py-2 rounded-lg transition-colors"
                      >
                        {q}
                      </button>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  )
}
