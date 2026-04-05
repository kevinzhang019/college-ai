import { motion } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { ChatMessage } from '../types'
import ConfidenceBadge from './ConfidenceBadge'
import SourceCard from './SourceCard'

export default function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === 'user'

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}
    >
      <div
        className={`max-w-[85%] ${
          isUser
            ? 'bg-amber-500 text-white rounded-2xl rounded-br-md px-4 py-3'
            : 'bg-white border border-amber-100 rounded-2xl rounded-bl-md px-4 py-3 shadow-warm-sm'
        }`}
      >
        {!isUser && (
          <div className="flex items-center gap-2 mb-2">
            <span className="text-lg">🎓</span>
            {message.confidence && (
              <ConfidenceBadge confidence={message.confidence} />
            )}
          </div>
        )}
        {isUser ? (
          <p className="text-sm leading-relaxed">{message.content}</p>
        ) : (
          <div className="markdown-answer text-sm text-warm-700">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {message.content}
            </ReactMarkdown>
          </div>
        )}
        {!isUser && message.sources && message.sources.length > 0 && (
          <div className="mt-3 space-y-1.5">
            <p className="text-xs text-warm-400 font-medium">Sources</p>
            {message.sources.slice(0, 3).map((source, i) => (
              <SourceCard key={source.url + i} source={source} index={i} />
            ))}
          </div>
        )}
      </div>
    </motion.div>
  )
}
