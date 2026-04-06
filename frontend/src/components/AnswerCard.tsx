import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeRaw from 'rehype-raw'
import type { AskResponse } from '../types'
import ConfidenceBadge from './ConfidenceBadge'
import SourceCard from './SourceCard'

export default function AnswerCard({ result }: { result: AskResponse }) {
  const answer = result.answer || ''
  const sources = result.sources || []
  const confidence = result.confidence && result.confidence in { high: 1, medium: 1, low: 1 }
    ? result.confidence
    : 'low'

  // Pre-process the answer to turn [N] citations into HTML before markdown
  const processed = answer.replace(
    /\[(\d+)\]/g,
    '<span class="citation-badge">$1</span>'
  )

  return (
    <div className="space-y-4">
      {/* Answer */}
      <div className="card p-6">
        <div className="flex items-center gap-3 mb-4">
          <span className="text-2xl">🎓</span>
          <div className="flex-1" />
          <ConfidenceBadge confidence={confidence} />
        </div>
        <div className="markdown-answer text-slate-300 leading-relaxed">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            rehypePlugins={[rehypeRaw]}
          >
            {processed}
          </ReactMarkdown>
        </div>
      </div>

      {/* Sources */}
      {sources.length > 0 && (
        <div>
          <h3 className="text-sm font-medium text-slate-500 mb-3 px-1">
            Sources ({result.source_count ?? sources.length})
          </h3>
          <div className="space-y-2">
            {sources.map((source, i) => (
              <SourceCard key={source.url + i} source={source} index={i} />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
