import { useEffect, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeRaw from 'rehype-raw'
import { useStore } from '../store'
import MessageBubble from './MessageBubble'
// SourceCard is used via MessageBubble

function StreamingMessage({ content }: { content: string }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className="flex justify-start"
    >
      <div className="max-w-[85%] bg-navy-900 border border-navy-700 rounded-2xl rounded-bl-md px-4 py-3 shadow-dark-sm">
        <div className="flex items-center gap-2 mb-2">
          <span className="text-lg">🎓</span>
          <span className="flex gap-1">
            <span className="w-1.5 h-1.5 bg-indigo-400 rounded-full animate-pulse" />
            <span className="w-1.5 h-1.5 bg-indigo-400 rounded-full animate-pulse [animation-delay:0.2s]" />
            <span className="w-1.5 h-1.5 bg-indigo-400 rounded-full animate-pulse [animation-delay:0.4s]" />
          </span>
        </div>
        <div className="markdown-answer text-sm text-slate-300">
          <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeRaw]}>
            {content}
          </ReactMarkdown>
        </div>
      </div>
    </motion.div>
  )
}

function WelcomeState() {
  const mode = useStore((s) => s.mode)

  const suggestions =
    mode === 'essay'
      ? [
          'Help me with my "Why Stanford?" essay',
          'Brainstorm ideas for MIT supplement',
          'What should I write about for Common App?',
        ]
      : [
          'What is the acceptance rate at MIT?',
          'Best scholarships for CS majors?',
          'Stanford application deadlines',
          'What GPA do I need for UCLA?',
        ]

  return (
    <div className="flex-1 flex flex-col items-center justify-center text-center px-6">
      <span className="text-5xl mb-4">{mode === 'essay' ? '✍️' : '🎒'}</span>
      <h2 className="text-xl font-medium text-slate-200 mb-2">
        {mode === 'essay' ? 'Essay Helper' : 'Ask me anything'}
      </h2>
      <p className="text-sm text-slate-500 mb-6 max-w-md">
        {mode === 'essay'
          ? 'I\'ll help you brainstorm essay ideas and review drafts using real college data.'
          : 'Ask about admissions, requirements, scholarships, deadlines, and more.'}
      </p>
      <div className="flex flex-wrap gap-2 justify-center max-w-lg">
        {suggestions.map((s) => (
          <button
            key={s}
            className="text-xs bg-navy-800/60 text-slate-400 px-3.5 py-2 rounded-full border border-navy-700 hover:border-indigo-500/40 hover:text-slate-200 transition-all"
          >
            {s}
          </button>
        ))}
      </div>
    </div>
  )
}

export default function ChatView() {
  const activeConversationId = useStore((s) => s.activeConversationId)
  const conversations = useStore((s) => s.conversations)
  const streamingContent = useStore((s) => s.streamingContent)
  const streamingLoading = useStore((s) => s.streamingLoading)
  const messagesEndRef = useRef<HTMLDivElement>(null)

  const conversation = activeConversationId
    ? conversations[activeConversationId]
    : null

  const messages = conversation?.messages || []

  // Auto-scroll on new messages / streaming
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages.length, streamingContent])

  if (!conversation || messages.length === 0) {
    if (!streamingLoading && !streamingContent) {
      return <WelcomeState />
    }
  }

  return (
    <div className="flex-1 overflow-y-auto custom-scrollbar px-4 py-6">
      <div className="max-w-3xl mx-auto space-y-4">
        <AnimatePresence>
          {messages.map((msg) => (
            <MessageBubble key={msg.id} message={msg} />
          ))}
        </AnimatePresence>

        {/* Streaming in-progress message */}
        {streamingContent && <StreamingMessage content={streamingContent} />}

        {/* Loading indicator before first token */}
        {streamingLoading && !streamingContent && (
          <motion.div
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            className="flex justify-start"
          >
            <div className="bg-navy-900 border border-navy-700 rounded-2xl rounded-bl-md px-4 py-3 shadow-dark-sm">
              <div className="flex items-center gap-2">
                <span className="text-lg">🎓</span>
                <span className="text-sm text-slate-500">Searching colleges...</span>
                <span className="flex gap-1">
                  <span className="w-1.5 h-1.5 bg-indigo-400 rounded-full dot-bounce" />
                  <span className="w-1.5 h-1.5 bg-indigo-400 rounded-full dot-bounce" />
                  <span className="w-1.5 h-1.5 bg-indigo-400 rounded-full dot-bounce" />
                </span>
              </div>
            </div>
          </motion.div>
        )}

        <div ref={messagesEndRef} />
      </div>
    </div>
  )
}
