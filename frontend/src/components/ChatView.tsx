import { useEffect, useMemo, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeRaw from 'rehype-raw'
import { useStore } from '../store'
import { useStreaming } from '../hooks/useStreaming'
import MessageBubble from './MessageBubble'
import { QA_SUGGESTIONS, ESSAY_SUGGESTIONS, pickRandom } from '../suggestions'

function ColeAvatar({ size = 'sm' }: { size?: 'sm' | 'lg' }) {
  const cls = size === 'lg' ? 'w-12 h-12 text-lg' : 'w-6 h-6 text-xs'
  return (
    <div className={`${cls} rounded-full bg-forest-600 flex items-center justify-center text-white font-bold shadow-dark-sm shrink-0`}>
      C
    </div>
  )
}

function StreamingMessage({ content }: { content: string }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className="flex justify-start"
    >
      <div className="w-full py-1">
        <div className="flex items-center gap-2 mb-1.5">
          <div className="w-5 h-5 rounded-full bg-forest-600 flex items-center justify-center text-white text-[10px] font-bold shadow-dark-sm shrink-0">C</div>
          <span className="text-sm font-semibold text-forest-400">Cole</span>
          <span className="flex gap-1">
            <span className="w-1.5 h-1.5 bg-forest-400 rounded-full animate-pulse" />
            <span className="w-1.5 h-1.5 bg-forest-400 rounded-full animate-pulse [animation-delay:0.2s]" />
            <span className="w-1.5 h-1.5 bg-forest-400 rounded-full animate-pulse [animation-delay:0.4s]" />
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
  const { send } = useStreaming()

  const suggestions = useMemo(
    () => pickRandom(mode === 'essay' ? ESSAY_SUGGESTIONS : QA_SUGGESTIONS, 4),
    [mode],
  )

  return (
    <div className="flex-1 flex flex-col items-center justify-center text-center px-6">
      <ColeAvatar size="lg" />
      <h2 className="text-xl font-medium text-slate-200 mt-4 mb-1">
        Hey, I'm Cole
      </h2>
      <p className="text-sm text-slate-500 mb-6 max-w-md">
        {mode === 'essay'
          ? "I'm your essay coach. I'll help you brainstorm ideas and review drafts using real college data."
          : "Your friendly college advisor. Ask me about admissions, requirements, scholarships, or deadlines."}
      </p>
      <div className="flex flex-wrap gap-2 justify-center max-w-lg">
        {suggestions.map((s) => (
          <button
            key={s}
            onClick={() => send(s)}
            className="text-xs bg-dark-800/60 text-slate-400 px-3.5 py-2 rounded-full border border-dark-700 hover:border-forest-500/40 hover:text-slate-200 transition-all"
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
            <div className="py-1">
              <div className="flex items-center gap-2">
                <div className="w-5 h-5 rounded-full bg-forest-600 flex items-center justify-center text-white text-[10px] font-bold shadow-dark-sm shrink-0">C</div>
                <span className="text-sm font-semibold text-forest-400">Cole</span>
                <span className="text-sm text-slate-500">is thinking...</span>
                <span className="flex gap-1">
                  <span className="w-1.5 h-1.5 bg-forest-400 rounded-full dot-bounce" />
                  <span className="w-1.5 h-1.5 bg-forest-400 rounded-full dot-bounce" />
                  <span className="w-1.5 h-1.5 bg-forest-400 rounded-full dot-bounce" />
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
