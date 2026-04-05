import { motion } from 'framer-motion'
import { useStore } from '../store'

const tabs = [
  { key: 'chat' as const, label: 'Brainstorm', icon: '💡' },
  { key: 'editor' as const, label: 'Review Draft', icon: '📝' },
]

export default function EssayTabSwitch() {
  const essayTab = useStore((s) => s.essayTab)
  const setEssayTab = useStore((s) => s.setEssayTab)

  return (
    <div className="flex justify-center mb-4">
      <div className="inline-flex bg-amber-50 rounded-full p-0.5 border border-amber-100">
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => setEssayTab(t.key)}
            className={`relative px-4 py-1.5 rounded-full text-xs font-medium transition-colors duration-200 ${
              essayTab === t.key
                ? 'text-amber-800'
                : 'text-warm-400 hover:text-warm-600'
            }`}
          >
            {essayTab === t.key && (
              <motion.div
                layoutId="essay-tab-pill"
                className="absolute inset-0 bg-white rounded-full shadow-warm-sm"
                transition={{ type: 'spring', bounce: 0.2, duration: 0.35 }}
              />
            )}
            <span className="relative z-10 flex items-center gap-1">
              <span>{t.icon}</span>
              {t.label}
            </span>
          </button>
        ))}
      </div>
    </div>
  )
}
