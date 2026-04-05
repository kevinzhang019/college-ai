const config = {
  high: {
    icon: '✓',
    label: 'High confidence',
    classes: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  },
  medium: {
    icon: '●',
    label: 'Medium confidence',
    classes: 'bg-amber-50 text-amber-700 border-amber-200',
  },
  low: {
    icon: '?',
    label: 'Low confidence',
    classes: 'bg-warm-100 text-warm-500 border-warm-200',
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
