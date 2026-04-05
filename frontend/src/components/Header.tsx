import { useStore } from '../store'

export default function Header() {
  const isConnected = useStore((s) => s.isConnected)

  return (
    <header className="text-center py-8 px-4">
      <div className="inline-flex items-center gap-3 mb-3">
        <span className="text-4xl">🎓</span>
        <h1 className="text-3xl font-bold text-warm-800">
          College AI
        </h1>
      </div>
      <p className="text-warm-500 text-lg max-w-md mx-auto">
        Your friendly guide to college admissions
      </p>
      <div className="flex items-center justify-center gap-2 mt-3 text-sm text-warm-400">
        <span
          className={`w-2 h-2 rounded-full ${
            isConnected ? 'bg-emerald-400 animate-pulse-soft' : 'bg-warm-300'
          }`}
        />
        <span>{isConnected ? 'Connected' : 'Connecting...'}</span>
      </div>
    </header>
  )
}
