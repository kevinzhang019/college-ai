export default function LoadingState({ message = 'Thinking...' }: { message?: string }) {
  return (
    <div className="flex flex-col items-center justify-center py-12 animate-fade-in">
      <div className="flex gap-1.5 mb-3">
        <span className="w-3 h-3 bg-blue-400 rounded-full dot-bounce" />
        <span className="w-3 h-3 bg-blue-500 rounded-full dot-bounce" />
        <span className="w-3 h-3 bg-blue-600 rounded-full dot-bounce" />
      </div>
      <p className="text-sm text-slate-500">{message}</p>
    </div>
  )
}
