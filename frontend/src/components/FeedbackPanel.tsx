import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeRaw from 'rehype-raw'
import type { AskResponse } from '../types'
import ConfidenceBadge from './ConfidenceBadge'
import SourceCard from './SourceCard'

export default function FeedbackPanel({
  feedback,
}: {
  feedback: AskResponse
}) {
  return (
    <div className="h-full overflow-y-auto custom-scrollbar p-5">
      <div className="flex items-center gap-2 mb-4">
        <span className="text-xl">🎓</span>
        <h3 className="text-sm font-semibold text-slate-200">AI Feedback</h3>
        <div className="flex-1" />
        <ConfidenceBadge confidence={feedback.confidence} />
      </div>
      <div className="markdown-answer text-sm text-slate-300 leading-relaxed">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          rehypePlugins={[rehypeRaw]}
        >
          {feedback.answer}
        </ReactMarkdown>
      </div>
      {feedback.sources.length > 0 && (
        <div className="mt-4">
          <p className="text-xs font-medium text-slate-500 mb-2">
            Sources used ({feedback.source_count})
          </p>
          <div className="space-y-1.5">
            {feedback.sources.slice(0, 4).map((source, i) => (
              <SourceCard key={source.url + i} source={source} index={i} />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
