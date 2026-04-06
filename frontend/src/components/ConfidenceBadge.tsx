const config = {
  high: {
    icon: '✓',
    label: 'High confidence',
    classes: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
  },
  medium: {
    icon: '●',
    label: 'Medium confidence',
    classes: 'bg-amber-500/15 text-amber-400 border-amber-500/30',
  },
  low: {
    icon: '?',
    label: 'Low confidence',
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
      className={`inline-flex items-center gap-1.5 px-3 py-1 text-xs font-medium rounded-full border ${c.classes}`}
    >
      <span className="text-[10px]">{c.icon}</span>
      {c.label}
    </span>
  )
}
