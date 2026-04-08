import { useState, useMemo } from 'react'
import { Combobox, ComboboxInput, ComboboxOption, ComboboxOptions, ComboboxButton } from '@headlessui/react'
import { useStore } from '../store'

interface Props {
  value: string | null
  onChange: (value: string | null) => void
  compact?: boolean
  showDefaultScreen?: boolean
  placeholder?: string
}

export default function CollegeCombobox({ value, onChange, compact, showDefaultScreen = true, placeholder = 'Select a school (optional)' }: Props) {
  const options = useStore((s) => s.collegeOptions)
  const savedSchools = useStore((s) => s.profile.savedSchools)
  const [query, setQuery] = useState('')

  const otherOptions = useMemo(
    () => options.filter((c) => !savedSchools.includes(c)),
    [options, savedSchools],
  )

  // Only savedSchools that actually exist in options
  const validSavedSchools = useMemo(
    () => savedSchools.filter((s) => options.includes(s)),
    [savedSchools, options],
  )

  const filtered = useMemo(() => {
    if (!query && showDefaultScreen) return null // null = sectioned default
    if (!query) return options.slice(0, 50)
    const lower = query.toLowerCase()
    return options.filter((c) => c.toLowerCase().includes(lower)).slice(0, 50)
  }, [query, options, showDefaultScreen])

  return (
    <Combobox value={value} onChange={onChange} onClose={() => setQuery('')} immediate>
      <div className="relative">
        <div className="relative">
          <ComboboxInput
            className={compact ? 'input-field-compact pr-8 text-sm' : 'input-field pr-8 text-sm'}
            placeholder={placeholder}
            displayValue={(val: string | null) => val || ''}
            onChange={(e) => setQuery(e.target.value)}
          />
          <ComboboxButton className="absolute inset-y-0 right-0 flex items-center pr-3">
            <svg className="w-4 h-4 text-slate-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          </ComboboxButton>
        </div>
        <ComboboxOptions className="absolute z-50 bottom-full mb-1 max-h-60 w-full overflow-auto rounded-xl bg-dark-900 shadow-dark-lg border border-dark-700 py-1">
          <ComboboxOption
            value={null}
            className="px-4 py-2 text-sm text-slate-500 cursor-pointer data-[focus]:bg-dark-800 data-[selected]:text-forest-400"
          >
            All colleges
          </ComboboxOption>

          {filtered === null ? (
            <>
              {validSavedSchools.length > 0 && (
                <>
                  <div className="px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-slate-500 select-none">
                    Your Schools
                  </div>
                  {validSavedSchools.map((c) => (
                    <ComboboxOption
                      key={c}
                      value={c}
                      className="px-4 py-2 text-sm text-slate-300 cursor-pointer data-[focus]:bg-dark-800 data-[selected]:text-forest-400 data-[selected]:font-medium"
                    >
                      {c}
                    </ComboboxOption>
                  ))}
                </>
              )}

              <div className="px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-slate-500 select-none">
                All Colleges
              </div>
              {otherOptions.slice(0, 50).map((c) => (
                <ComboboxOption
                  key={c}
                  value={c}
                  className="px-4 py-2 text-sm text-slate-300 cursor-pointer data-[focus]:bg-dark-800 data-[selected]:text-forest-400 data-[selected]:font-medium"
                >
                  {c}
                </ComboboxOption>
              ))}
            </>
          ) : filtered.length === 0 ? (
            <div className="px-4 py-2 text-sm text-slate-500">No schools found.</div>
          ) : (
            filtered.map((c) => (
              <ComboboxOption
                key={c}
                value={c}
                className="px-4 py-2 text-sm text-slate-300 cursor-pointer data-[focus]:bg-dark-800 data-[selected]:text-forest-400 data-[selected]:font-medium"
              >
                {c}
              </ComboboxOption>
            ))
          )}
        </ComboboxOptions>
      </div>
    </Combobox>
  )
}
