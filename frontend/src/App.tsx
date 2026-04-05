import { AnimatePresence, motion } from 'framer-motion'
import { useStore } from './store'
import { useApi } from './hooks/useApi'
import Header from './components/Header'
import ModeSwitch from './components/ModeSwitch'
import FilterBar from './components/FilterBar'
import QuestionInput from './components/QuestionInput'
import LoadingState from './components/LoadingState'
import AnswerCard from './components/AnswerCard'
import EssayTabSwitch from './components/EssayTabSwitch'
import EssayChatTab from './components/EssayChatTab'
import EssayEditorTab from './components/EssayEditorTab'
import HelpModal from './components/HelpModal'
import HelpButton from './components/HelpButton'
import ErrorBoundary from './components/ErrorBoundary'

function QAMode() {
  const qaLoading = useStore((s) => s.qaLoading)
  const qaResult = useStore((s) => s.qaResult)

  return (
    <div className="max-w-2xl mx-auto space-y-6 px-4">
      <QuestionInput />

      {qaLoading && <LoadingState message="Searching colleges..." />}

      {!qaLoading && qaResult && (
        <motion.div
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.3 }}
        >
          <AnswerCard result={qaResult} />
        </motion.div>
      )}

      {!qaLoading && !qaResult && <WelcomeState />}
    </div>
  )
}

function WelcomeState() {
  const setQaQuestion = useStore((s) => s.setQaQuestion)

  const suggestions = [
    'What is the acceptance rate at MIT?',
    'Best scholarships for CS majors?',
    'Stanford application deadlines',
    'What GPA do I need for UCLA?',
  ]

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className="text-center py-8"
    >
      <span className="text-5xl mb-4 block">🎒</span>
      <h2 className="text-lg font-medium text-warm-700 mb-2">
        What would you like to know?
      </h2>
      <p className="text-sm text-warm-400 mb-6 max-w-sm mx-auto">
        Ask me anything about college admissions, requirements, scholarships, or
        deadlines.
      </p>
      <div className="flex flex-wrap gap-2 justify-center max-w-lg mx-auto">
        {suggestions.map((s) => (
          <button
            key={s}
            onClick={() => setQaQuestion(s)}
            className="text-xs bg-white text-warm-600 px-3 py-2 rounded-full border border-amber-100 shadow-warm-sm hover:shadow-warm hover:border-amber-200 transition-all"
          >
            {s}
          </button>
        ))}
      </div>
    </motion.div>
  )
}

function EssayMode() {
  const essayTab = useStore((s) => s.essayTab)

  return (
    <div className="px-4">
      <EssayTabSwitch />
      <AnimatePresence mode="wait">
        {essayTab === 'chat' ? (
          <motion.div
            key="chat"
            initial={{ opacity: 0, x: -10 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: 10 }}
            transition={{ duration: 0.2 }}
          >
            <EssayChatTab />
          </motion.div>
        ) : (
          <motion.div
            key="editor"
            initial={{ opacity: 0, x: 10 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: -10 }}
            transition={{ duration: 0.2 }}
          >
            <EssayEditorTab />
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

export default function App() {
  useApi()
  const mode = useStore((s) => s.mode)

  return (
    <div className="min-h-screen bg-cream">
      <ErrorBoundary>
      <div className="max-w-6xl mx-auto pb-20">
        <Header />
        <ModeSwitch />
        <FilterBar />

        <AnimatePresence mode="wait">
          {mode === 'qa' ? (
            <motion.div
              key="qa"
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -10 }}
              transition={{ duration: 0.25 }}
            >
              <QAMode />
            </motion.div>
          ) : (
            <motion.div
              key="essay"
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -10 }}
              transition={{ duration: 0.25 }}
            >
              <EssayMode />
            </motion.div>
          )}
        </AnimatePresence>
      </div>
      </ErrorBoundary>

      <HelpButton />
      <HelpModal />
    </div>
  )
}
