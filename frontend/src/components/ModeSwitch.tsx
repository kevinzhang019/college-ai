import { motion } from 'framer-motion'
import { useStore } from '../store'

const modes = [
  { key: 'qa' as const, label: 'Q&A', icon: '💬' },
  { key: 'essay' as const, label: 'Essay Helper', icon: '✍️' },
]

export default function ModeSwitch() {
  const mode = useStore((s) => s.mode)
  const setMode = useStore((s) => s.setMode)

  return (
    <div className="flex justify-center mb-6">
      <div className="inline-flex bg-white rounded-full p-1 shadow-warm-sm border border-amber-100">
        {modes.map((m) => (
          <button
            key={m.key}
            onClick={() => setMode(m.key)}
            className={`relative px-5 py-2 rounded-full text-sm font-medium transition-colors duration-200 ${
              mode === m.key ? 'text-white' : 'text-warm-500 hover:text-warm-700'
            }`}
          >
            {mode === m.key && (
              <motion.div
                layoutId="mode-pill"
                className="absolute inset-0 bg-amber-500 rounded-full"
                transition={{ type: 'spring', bounce: 0.2, duration: 0.4 }}
              />
            )}
            <span className="relative z-10 flex items-center gap-1.5">
              <span>{m.icon}</span>
              {m.label}
            </span>
          </button>
        ))}
      </div>
    </div>
  )
}
