import { motion, AnimatePresence } from 'framer-motion'
import { useStore } from '../store'
import ConversationList from './ConversationList'
import type { AppMode } from '../types'

const modes: { key: AppMode; label: string; icon: string }[] = [
  { key: 'qa', label: 'Chat', icon: '💬' },
  { key: 'admissions', label: 'Admissions', icon: '🎯' },
  { key: 'experiences', label: 'My Profile', icon: '📋' },
]

export default function Sidebar() {
  const mode = useStore((s) => s.mode)
  const setMode = useStore((s) => s.setMode)
  const sidebarOpen = useStore((s) => s.sidebarOpen)
  const setSidebarOpen = useStore((s) => s.setSidebarOpen)
  const createConversation = useStore((s) => s.createConversation)

  const handleNewChat = () => {
    if (mode === 'experiences' || mode === 'admissions') return
    createConversation('qa')
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
        className="fixed lg:relative z-50 lg:z-auto w-[280px] h-screen flex flex-col bg-dark-900 border-r border-dark-700 shrink-0"
      >
        {/* Header */}
        <div className="p-4 border-b border-dark-700">
          <div className="flex items-center justify-between mb-4">
            <div className="flex items-center gap-2.5">
              <div className="w-8 h-8 rounded-full bg-forest-600 flex items-center justify-center text-white text-sm font-bold shadow-dark-sm">
                C
              </div>
              <div>
                <h1 className="text-base font-semibold text-slate-100 tracking-tight leading-none">
                  Cole
                </h1>
                <p className="text-[10px] text-slate-500 mt-0.5">Your college advisor</p>
              </div>
            </div>
            <button
              onClick={() => setSidebarOpen(false)}
              className="lg:hidden p-1 text-slate-400 hover:text-slate-200"
            >
              <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>

          {/* New chat button */}
          {mode === 'qa' && (
            <button
              onClick={handleNewChat}
              className="w-full flex items-center gap-2 px-3 py-2 rounded-lg border border-dark-700 text-sm text-slate-300 hover:bg-dark-800 hover:border-slate-600 transition-all mb-3"
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
              </svg>
              New Chat
            </button>
          )}

          {/* Vertical mode selector */}
          <div className="space-y-1">
            {modes.map((m) => (
              <button
                key={m.key}
                onClick={() => setMode(m.key)}
                className={`w-full flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium transition-all ${
                  mode === m.key
                    ? 'bg-forest-600/15 text-forest-300 border border-forest-500/20'
                    : 'text-slate-400 hover:text-slate-200 hover:bg-dark-800 border border-transparent'
                }`}
              >
                <span className="text-base">{m.icon}</span>
                {m.label}
              </button>
            ))}
          </div>
        </div>

        {/* Conversation list */}
        {mode === 'qa' && (
          <div className="flex-1 overflow-y-auto custom-scrollbar">
            <ConversationList />
          </div>
        )}

        {/* Experiences hint */}
        {mode === 'experiences' && (
          <div className="flex-1 flex items-center justify-center p-6 text-center">
            <p className="text-sm text-slate-500">
              Add your activities, projects, and experiences. Cole will use
              them as context when helping with essays.
            </p>
          </div>
        )}

        {/* Admissions hint */}
        {mode === 'admissions' && (
          <div className="flex-1 flex items-center justify-center p-6 text-center">
            <p className="text-sm text-slate-500">
              Add schools and see your estimated admission chances based on your academic profile.
            </p>
          </div>
        )}
      </motion.aside>
    </>
  )
}
