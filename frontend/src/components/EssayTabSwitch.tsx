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
      <div className="inline-flex bg-navy-800 rounded-full p-0.5 border border-navy-700">
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => setEssayTab(t.key)}
            className={`relative px-4 py-1.5 rounded-full text-xs font-medium transition-colors duration-200 ${
              essayTab === t.key
                ? 'text-slate-100'
                : 'text-slate-500 hover:text-slate-300'
            }`}
          >
            {essayTab === t.key && (
              <motion.div
                layoutId="essay-tab-pill"
                className="absolute inset-0 bg-navy-900 rounded-full shadow-dark-sm border border-navy-700"
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
