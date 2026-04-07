const config = {
  high: {
    icon: '✓',
    label: 'High confidence',
    hint: '',
    dot: 'bg-emerald-400',
    classes: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
  },
  medium: {
    icon: '●',
    label: 'Medium confidence',
    hint: 'Consider verifying with the school',
    dot: 'bg-amber-400',
    classes: 'bg-amber-500/15 text-amber-400 border-amber-500/30',
  },
  low: {
    icon: '?',
    label: 'Low confidence',
    hint: 'We recommend checking with the school',
    dot: 'bg-slate-400',
    classes: 'bg-slate-500/15 text-slate-400 border-slate-500/30',
  },
}

export default function ConfidenceBadge({
  confidence,
}: {
  confidence: 'high' | 'medium' | 'low'
}) {
  const c = config[confidence] || config.low
  return (
    <span
      className={`group inline-flex items-center gap-0 px-1.5 py-1 text-xs font-medium rounded-full border cursor-default hover:gap-1.5 hover:px-3 transition-all duration-300 ease-in-out ${c.classes}`}
    >
      <span className={`w-2 h-2 rounded-full shrink-0 ${c.dot}`} />
      <span className="max-w-0 opacity-0 overflow-hidden whitespace-nowrap transition-all duration-300 ease-in-out group-hover:max-w-[350px] group-hover:opacity-100">
        {c.label}{c.hint && <span className="text-[10px] opacity-75"> · {c.hint}</span>}
      </span>
    </span>
  )
}
