import { useStore } from '../store'
import CollegeCombobox from './CollegeCombobox'

const topKOptions = [5, 8, 12, 20]

export default function FilterBar() {
  const topK = useStore((s) => s.topK)
  const setTopK = useStore((s) => s.setTopK)

  return (
    <div className="flex flex-col sm:flex-row gap-3 mb-6 max-w-4xl mx-auto px-4">
      <div className="flex-1">
        <label className="block text-xs font-medium text-slate-400 mb-1 ml-1">
          Filter by college
        </label>
        <CollegeCombobox />
      </div>
      <div className="w-full sm:w-32">
        <label className="block text-xs font-medium text-slate-400 mb-1 ml-1">
          Sources
        </label>
        <select
          value={topK}
          onChange={(e) => setTopK(Number(e.target.value))}
          className="input-field text-sm"
        >
          {topKOptions.map((k) => (
            <option key={k} value={k}>
              {k} results
            </option>
          ))}
        </select>
      </div>
    </div>
  )
}
