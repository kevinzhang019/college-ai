export default function Header() {
  return (
    <header className="text-center py-8 px-4">
      <div className="inline-flex items-center gap-3 mb-3">
        <span className="text-4xl">🎓</span>
        <h1 className="text-3xl font-bold text-slate-100">
          College AI
        </h1>
      </div>
      <p className="text-slate-400 text-lg max-w-md mx-auto">
        Your friendly guide to college admissions
      </p>
    </header>
  )
}
