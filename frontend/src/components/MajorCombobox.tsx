import { useState, useMemo } from 'react'
import { Combobox, ComboboxInput, ComboboxOption, ComboboxOptions, ComboboxButton } from '@headlessui/react'
import { useStore } from '../store'
import { ALLOWED_MAJORS } from '../types'

interface Props {
  value: string | null
  onChange: (value: string | null) => void
  compact?: boolean
}

export default function MajorCombobox({ value, onChange, compact }: Props) {
  const preferredMajors = useStore((s) => s.profile.preferredMajors)
  const [query, setQuery] = useState('')

  const byName = (a: string, b: string) =>
    a.localeCompare(b, undefined, { sensitivity: 'base' })

  const sortedPreferredMajors = useMemo(
    () => preferredMajors.slice().sort(byName),
    [preferredMajors],
  )

  const otherMajors = useMemo(
    () => ALLOWED_MAJORS.filter((m) => !preferredMajors.includes(m)).sort(byName),
    [preferredMajors],
  )

  const filtered = useMemo(() => {
    if (!query) return null // null = show sectioned default
    const lower = query.toLowerCase()
    return ALLOWED_MAJORS.filter((m) => m.toLowerCase().includes(lower)).sort(byName)
  }, [query])

  return (
    <Combobox
      value={value}
      onChange={(v) => { onChange(v); setQuery('') }}
      onClose={() => setQuery('')}
      immediate
    >
      <div className="relative">
        <div className="relative">
          <ComboboxInput
            className={
              compact
                ? 'input-field-compact text-xs py-1.5 w-28 pr-6 truncate'
                : 'input-field-compact text-sm pr-8'
            }
            placeholder={compact ? 'Major' : 'Not specified'}
            displayValue={(val: string | null) => val || ''}
            onChange={(e) => setQuery(e.target.value)}
          />
          <ComboboxButton className={`absolute inset-y-0 right-0 flex items-center ${compact ? 'pr-1.5' : 'pr-3'}`}>
            <svg className={`${compact ? 'w-3 h-3' : 'w-4 h-4'} text-slate-500`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          </ComboboxButton>
        </div>

        <ComboboxOptions
          className={`absolute z-50 ${compact ? 'right-0 w-56' : 'w-full'} max-h-60 overflow-auto rounded-xl bg-dark-900 shadow-dark-lg border border-dark-700 py-1`}
          anchor={compact ? 'bottom end' : undefined}
        >
          {/* "Not specified" / clear option */}
          <ComboboxOption
            value={null}
            className="px-3 py-1.5 text-sm text-slate-500 cursor-pointer data-[focus]:bg-dark-800 data-[selected]:text-forest-400"
          >
            {compact ? 'No major' : 'Not specified'}
          </ComboboxOption>

          {filtered === null ? (
            <>
              {/* Sectioned default: preferred majors first, then the rest */}
              {sortedPreferredMajors.length > 0 && (
                <>
                  <div className="px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-slate-500 select-none">
                    Your Majors
                  </div>
                  {sortedPreferredMajors.map((m) => (
                    <ComboboxOption
                      key={m}
                      value={m}
                      className="px-3 py-1.5 text-sm text-slate-300 cursor-pointer data-[focus]:bg-dark-800 data-[selected]:text-forest-400 data-[selected]:font-medium"
                    >
                      {m}
                    </ComboboxOption>
                  ))}
                </>
              )}

              <div className="px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-slate-500 select-none">
                All Majors
              </div>
              {otherMajors.map((m) => (
                <ComboboxOption
                  key={m}
                  value={m}
                  className="px-3 py-1.5 text-sm text-slate-300 cursor-pointer data-[focus]:bg-dark-800 data-[selected]:text-forest-400 data-[selected]:font-medium"
                >
                  {m}
                </ComboboxOption>
              ))}
            </>
          ) : filtered.length === 0 ? (
            <div className="px-3 py-2 text-sm text-slate-500">No majors found.</div>
          ) : (
            filtered.map((m) => (
              <ComboboxOption
                key={m}
                value={m}
                className="px-3 py-1.5 text-sm text-slate-300 cursor-pointer data-[focus]:bg-dark-800 data-[selected]:text-forest-400 data-[selected]:font-medium"
              >
                {m}
              </ComboboxOption>
            ))
          )}
        </ComboboxOptions>
      </div>
    </Combobox>
  )
}
