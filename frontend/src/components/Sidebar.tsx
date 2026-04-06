import { motion, AnimatePresence } from 'framer-motion'
import { useStore } from '../store'
import ConversationList from './ConversationList'
import type { AppMode } from '../types'

const modes: { key: AppMode; label: string; icon: string }[] = [
  { key: 'qa', label: 'Q&A', icon: '💬' },
  { key: 'essay', label: 'Essay', icon: '✍️' },
  { key: 'experiences', label: 'Profile', icon: '📋' },
]

export default function Sidebar() {
  const mode = useStore((s) => s.mode)
  const setMode = useStore((s) => s.setMode)
  const sidebarOpen = useStore((s) => s.sidebarOpen)
  const setSidebarOpen = useStore((s) => s.setSidebarOpen)
  const createConversation = useStore((s) => s.createConversation)

  const handleNewChat = () => {
    if (mode === 'experiences') return
    createConversation(mode === 'essay' ? 'essay' : 'qa')
  }

  return (
    <>
      {/* Mobile overlay */}
      <AnimatePresence>
        {sidebarOpen && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 bg-black/50 z-40 lg:hidden"
            onClick={() => setSidebarOpen(false)}
          />
        )}
      </AnimatePresence>

      {/* Sidebar panel */}
      <motion.aside
        initial={false}
        animate={{ x: sidebarOpen ? 0 : -280 }}
        transition={{ type: 'spring', damping: 25, stiffness: 300 }}
        className="fixed lg:relative z-50 lg:z-auto w-[280px] h-screen flex flex-col bg-navy-900 border-r border-navy-700 shrink-0"
      >
        {/* Header */}
        <div className="p-4 border-b border-navy-700">
          <div className="flex items-center justify-between mb-4">
            <h1 className="text-lg font-semibold text-slate-100 tracking-tight">
              College AI
            </h1>
            <button
              onClick={() => setSidebarOpen(false)}
              className="lg:hidden p-1 text-slate-400 hover:text-slate-200"
            >
              <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>

          {/* Mode selector */}
          <div className="flex gap-1 p-1 bg-navy-950 rounded-xl">
            {modes.map((m) => (
              <button
                key={m.key}
                onClick={() => setMode(m.key)}
                className={`flex-1 flex items-center justify-center gap-1.5 px-2 py-2 rounded-lg text-xs font-medium transition-all ${
                  mode === m.key
                    ? 'bg-indigo-600 text-white shadow-dark-sm'
                    : 'text-slate-400 hover:text-slate-200 hover:bg-navy-800'
                }`}
              >
                <span className="text-sm">{m.icon}</span>
                {m.label}
              </button>
            ))}
          </div>
        </div>

        {/* New chat button */}
        {mode !== 'experiences' && (
          <div className="p-3">
            <button
              onClick={handleNewChat}
              className="w-full flex items-center gap-2 px-4 py-2.5 rounded-xl border border-navy-700 text-sm text-slate-300 hover:bg-navy-800 hover:border-slate-600 transition-all"
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
              </svg>
              New Chat
            </button>
          </div>
        )}

        {/* Conversation list */}
        {mode !== 'experiences' && (
          <div className="flex-1 overflow-y-auto custom-scrollbar">
            <ConversationList />
          </div>
        )}

        {/* Experiences hint */}
        {mode === 'experiences' && (
          <div className="flex-1 flex items-center justify-center p-6 text-center">
            <p className="text-sm text-slate-500">
              Add your activities, projects, and experiences. They'll be included
              as context when you use Essay mode.
            </p>
          </div>
        )}
      </motion.aside>
    </>
  )
}
