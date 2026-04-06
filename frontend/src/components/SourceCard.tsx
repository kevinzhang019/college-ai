import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import type { Source } from '../types'

export default function SourceCard({
  source,
  index,
}: {
  source: Source
  index: number
}) {
  const [expanded, setExpanded] = useState(false)
  const content = source.content || ''
  const snippet = content.slice(0, 200)
  const hasMore = content.length > 200

  return (
    <motion.div
      id={`source-${index}`}
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.05 }}
      className="card-hover border-l-4 border-l-blue-500 p-4 cursor-pointer"
      onClick={() => setExpanded(!expanded)}
    >
      <div className="min-w-0">
        <div className="flex items-center gap-2 mb-1">
          <span className="text-xs font-medium text-blue-400 bg-blue-500/10 px-2 py-0.5 rounded-full">
            {source.college_name || 'Unknown'}
          </span>
          {source.page_type && (
            <span className="text-xs text-slate-500">{source.page_type}</span>
          )}
        </div>
        <a
          href={source.url}
          target="_blank"
          rel="noopener noreferrer"
          className="text-sm font-medium text-slate-200 hover:text-blue-400 transition-colors line-clamp-1"
          onClick={(e) => e.stopPropagation()}
        >
          {source.title || source.url}
        </a>
        <AnimatePresence initial={false}>
          <motion.p
            key={expanded ? 'full' : 'snippet'}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            className="text-xs text-slate-500 mt-1 leading-relaxed"
          >
            {expanded ? content : snippet}
            {!expanded && hasMore && '...'}
          </motion.p>
        </AnimatePresence>
      </div>
    </motion.div>
  )
}
