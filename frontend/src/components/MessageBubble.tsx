import { motion } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeRaw from 'rehype-raw'
import type { ChatMessage } from '../types'
import ConfidenceBadge from './ConfidenceBadge'
import SourceCard from './SourceCard'

export default function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === 'user'

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className={`flex gap-3 ${isUser ? 'justify-end' : 'justify-start'}`}
    >
      {isUser ? (
        <div className="max-w-[85%] bg-forest-600 text-white rounded-2xl rounded-br-md px-4 py-3">
          <p className="text-sm leading-relaxed">{message.content}</p>
        </div>
      ) : (
        <div className="w-full py-1">
          <div className="flex items-center gap-2 mb-1.5">
            <span className="text-xs font-medium text-forest-400">Cole</span>
            {message.confidence && (
              <ConfidenceBadge confidence={message.confidence} />
            )}
          </div>
          <div className="markdown-answer text-sm text-slate-300">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              rehypePlugins={[rehypeRaw]}
            >
              {message.content}
            </ReactMarkdown>
          </div>
          {message.sources && message.sources.length > 0 && (
            <div className="mt-3 space-y-1.5">
              <p className="text-xs text-slate-500 font-medium">Sources</p>
              {message.sources.slice(0, 3).map((source, i) => (
                <SourceCard key={source.url + i} source={source} index={i} />
              ))}
            </div>
          )}
        </div>
      )}
    </motion.div>
  )
}
