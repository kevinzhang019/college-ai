import { motion } from 'framer-motion'
import type { PredictionResult } from '../types'
import { formatSchoolName } from '../lib/format'

const CLASS_STYLES: Record<string, string> = {
  safety: 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/30',
  match: 'bg-amber-500/15 text-amber-400 border border-amber-500/30',
  reach: 'bg-rose-500/15 text-rose-400 border border-rose-500/30',
}

const CLASS_LABELS: Record<string, string> = {
  safety: 'Safety',
  match: 'Match',
  reach: 'Reach',
}

const PROB_COLORS: Record<string, string> = {
  safety: 'text-emerald-400',
  match: 'text-amber-400',
  reach: 'text-rose-400',
}

interface Props {
  result: PredictionResult
  index?: number
}

export default function PredictionCard({ result, index = 0 }: Props) {
  if (result.error) {
    return (
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: index * 0.05 }}
        className="card p-4 opacity-60"
      >
        <div className="flex items-center justify-between">
          <span className="text-sm font-medium text-slate-100">{formatSchoolName(result.school_name)}</span>
          <span className="text-xs text-red-400">{result.error}</span>
        </div>
      </motion.div>
    )
  }

  const pct = Math.round(result.probability * 100)

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.05 }}
      className="card p-4"
    >
      <div className="flex items-start justify-between gap-3">
        {/* Left: school info */}
        <div className="flex-1 min-w-0">
          <h4 className="text-sm font-medium text-slate-100 truncate">{formatSchoolName(result.school_name)}</h4>
          {result.school_acceptance_rate != null && (
            <p className="text-xs text-slate-500 mt-0.5">
              {Math.round(result.school_acceptance_rate * 100)}% acceptance rate
            </p>
          )}
        </div>

        {/* Right: probability + badge */}
        <div className="text-right shrink-0">
          <span className={`text-2xl font-bold ${PROB_COLORS[result.classification] || 'text-slate-100'}`}>
            {pct}%
          </span>
          <div className="mt-1">
            <span className={`text-[10px] font-semibold px-2 py-0.5 rounded-full ${CLASS_STYLES[result.classification] || ''}`}>
              {CLASS_LABELS[result.classification] || result.classification}
            </span>
          </div>
        </div>
      </div>

      {/* Confidence interval */}
      <p className="text-[10px] text-slate-500 mt-2">
        95% CI: {Math.round(result.confidence_interval[0] * 100)}% – {Math.round(result.confidence_interval[1] * 100)}%
      </p>

      {/* Factors */}
      {result.factors.length > 0 && (
        <div className="mt-2 space-y-1">
          {result.factors.map((f, i) => (
            <div key={i} className="flex items-center gap-1.5 text-xs">
              <span className={f.impact === 'positive' ? 'text-emerald-400' : 'text-rose-400'}>
                {f.impact === 'positive' ? '▲' : '▼'}
              </span>
              <span className="text-slate-400">{f.detail}</span>
            </div>
          ))}
        </div>
      )}
    </motion.div>
  )
}
