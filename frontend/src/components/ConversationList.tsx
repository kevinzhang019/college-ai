import { motion } from 'framer-motion'
import { useStore } from '../store'

function relativeTime(ts: number): string {
  const diff = Date.now() - ts
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  if (days < 7) return `${days}d ago`
  return new Date(ts).toLocaleDateString()
}

export default function ConversationList() {
  const conversations = useStore((s) => s.conversations)
  const conversationOrder = useStore((s) => s.conversationOrder)
  const activeConversationId = useStore((s) => s.activeConversationId)
  const setActiveConversation = useStore((s) => s.setActiveConversation)
  const deleteConversation = useStore((s) => s.deleteConversation)
  const mode = useStore((s) => s.mode)

  // Filter conversations by current mode
  const filtered = conversationOrder
    .map((id) => conversations[id])
    .filter((c) => c && c.mode === (mode === 'essay' ? 'essay' : 'qa'))

  if (filtered.length === 0) {
    return (
      <div className="px-4 py-8 text-center">
        <p className="text-xs text-slate-600">No conversations yet</p>
      </div>
    )
  }

  return (
    <div className="px-2 py-1 space-y-0.5">
      {filtered.map((conv) => (
        <motion.button
          key={conv.id}
          layout
          onClick={() => setActiveConversation(conv.id)}
          className={`w-full group flex items-center gap-2 px-3 py-2.5 rounded-lg text-left transition-colors ${
            activeConversationId === conv.id
              ? 'bg-forest-600/20 text-slate-100'
              : 'text-slate-400 hover:bg-dark-800 hover:text-slate-200'
          }`}
        >
          <div className="flex-1 min-w-0">
            <p className="text-sm truncate">{conv.title}</p>
            <p className="text-xs text-slate-600 mt-0.5">
              {relativeTime(conv.updatedAt)}
            </p>
          </div>
          <button
            onClick={(e) => {
              e.stopPropagation()
              deleteConversation(conv.id)
            }}
            className="shrink-0 opacity-0 group-hover:opacity-100 p-1 text-slate-600 hover:text-red-400 transition-all"
          >
            <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
            </svg>
          </button>
        </motion.button>
      ))}
    </div>
  )
}
