import { useState, useMemo } from 'react'
import { Combobox, ComboboxInput, ComboboxOption, ComboboxOptions, ComboboxButton } from '@headlessui/react'
import { useStore } from '../store'

export default function CollegeCombobox() {
  const college = useStore((s) => s.college)
  const setCollege = useStore((s) => s.setCollege)
  const options = useStore((s) => s.collegeOptions)
  const [query, setQuery] = useState('')

  const filtered = useMemo(() => {
    if (!query) return options.slice(0, 50)
    const lower = query.toLowerCase()
    return options.filter((c) => c.toLowerCase().includes(lower)).slice(0, 50)
  }, [query, options])

  return (
    <Combobox value={college} onChange={setCollege} onClose={() => setQuery('')}>
      <div className="relative">
        <div className="relative">
          <ComboboxInput
            className="input-field pr-8 text-sm"
            placeholder="All colleges"
            displayValue={(val: string | null) => val || ''}
            onChange={(e) => setQuery(e.target.value)}
          />
          <ComboboxButton className="absolute inset-y-0 right-0 flex items-center pr-3">
            <svg className="w-4 h-4 text-slate-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          </ComboboxButton>
        </div>
        <ComboboxOptions className="absolute z-50 mt-1 max-h-60 w-full overflow-auto rounded-xl bg-navy-900 shadow-dark-lg border border-navy-700 py-1">
          <ComboboxOption
            value={null}
            className="px-4 py-2 text-sm text-slate-500 cursor-pointer data-[focus]:bg-navy-800 data-[selected]:text-blue-400"
          >
            All colleges
          </ComboboxOption>
          {filtered.map((c) => (
            <ComboboxOption
              key={c}
              value={c}
              className="px-4 py-2 text-sm text-slate-300 cursor-pointer data-[focus]:bg-navy-800 data-[selected]:text-blue-400 data-[selected]:font-medium"
            >
              {c}
            </ComboboxOption>
          ))}
        </ComboboxOptions>
      </div>
    </Combobox>
  )
}
