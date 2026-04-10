import { useState, useMemo, useRef, useCallback } from 'react'
import { Combobox, ComboboxInput, ComboboxOption, ComboboxOptions, ComboboxButton } from '@headlessui/react'
import { useStore } from '../store'
import { formatSchoolName } from '../lib/format'

const MAX_RESULTS = 100

interface Props {
  value: string | null
  onChange: (value: string | null) => void
  compact?: boolean
  showDefaultScreen?: boolean
  placeholder?: string
  reopenOnSelect?: boolean
  excludeValues?: string[]
}

export default function CollegeCombobox({ value, onChange, compact, showDefaultScreen = true, placeholder = 'Select a school (optional)', reopenOnSelect, excludeValues }: Props) {
  const options = useStore((s) => s.collegeOptions)
  const savedSchools = useStore((s) => s.profile.savedSchools)
  const [query, setQuery] = useState('')
  const inputRef = useRef<HTMLInputElement | null>(null)

  const excluded = useMemo(
    () => excludeValues ? new Set(excludeValues) : null,
    [excludeValues],
  )

  const handleChange = useCallback((val: string | null) => {
    onChange(val)
    if (reopenOnSelect && val !== null) {
      inputRef.current?.blur()
      setTimeout(() => inputRef.current?.focus(), 100)
    }
  }, [onChange, reopenOnSelect])

  const byName = (a: string, b: string) =>
    formatSchoolName(a).localeCompare(formatSchoolName(b), undefined, { sensitivity: 'base' })

  const validSavedSchools = useMemo(
    () =>
      savedSchools
        .filter((s) => options.includes(s) && (!excluded || !excluded.has(s)))
        .slice()
        .sort(byName),
    [savedSchools, options, excluded],
  )

  const otherSchoolsFull = useMemo(() => {
    const base = excluded ? options.filter((c) => !excluded.has(c)) : options
    const savedSet = new Set(validSavedSchools)
    return base.filter((c) => !savedSet.has(c)).sort(byName)
  }, [options, excluded, validSavedSchools])

  const otherSchools = useMemo(() => {
    const room = Math.max(0, MAX_RESULTS - validSavedSchools.length)
    return otherSchoolsFull.slice(0, room)
  }, [otherSchoolsFull, validSavedSchools])

  const browseTruncated = otherSchoolsFull.length > otherSchools.length

  const filtered = useMemo(() => {
    if (!query) return null
    const lower = query.toLowerCase()
    const base = excluded ? options.filter((c) => !excluded.has(c)) : options
    return base.filter((c) => c.toLowerCase().includes(lower)).sort(byName)
  }, [query, options, excluded])

  const filteredCapped = useMemo(
    () => (filtered ? filtered.slice(0, MAX_RESULTS) : null),
    [filtered],
  )
  const searchTruncated = filtered !== null && filtered.length > MAX_RESULTS

  const optionClass = 'px-3 py-1.5 text-sm text-slate-300 cursor-pointer data-[focus]:bg-dark-800 data-[selected]:text-forest-400 data-[selected]:font-medium'
  const sectionHeaderClass = 'px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-slate-500 select-none'
  const hintClass = 'px-3 py-1.5 text-xs text-slate-500 italic select-none'

  return (
    <Combobox
      value={value}
      onChange={handleChange}
      onClose={() => setQuery('')}
      immediate
    >
      <div className="relative">
        <div className="relative">
          <ComboboxInput
            ref={inputRef}
            className={compact ? 'input-field-compact pr-14 text-sm' : 'input-field pr-14 text-sm'}
            placeholder={placeholder}
            displayValue={(val: string | null) => val || ''}
            onChange={(e) => setQuery(e.target.value)}
          />
          {value !== null && (
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation()
                handleChange(null)
              }}
              className="absolute inset-y-0 right-7 flex items-center text-slate-500 hover:text-slate-300"
              aria-label="Clear selection"
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          )}
          <ComboboxButton className="absolute inset-y-0 right-0 flex items-center pr-3">
            <svg className="w-4 h-4 text-slate-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          </ComboboxButton>
        </div>
        <ComboboxOptions className="absolute z-50 bottom-full mb-1 max-h-60 w-full overflow-auto rounded-xl bg-dark-900 shadow-dark-lg border border-dark-700 py-1">
          {showDefaultScreen && (
            <ComboboxOption
              value={null}
              className="px-3 py-1.5 text-sm text-slate-500 cursor-pointer data-[focus]:bg-dark-800 data-[selected]:text-forest-400"
            >
              Not Specified
            </ComboboxOption>
          )}

          {filteredCapped === null ? (
            showDefaultScreen ? (
              <>
                {validSavedSchools.length > 0 && (
                  <>
                    <div className={sectionHeaderClass}>My Schools</div>
                    {validSavedSchools.map((c) => (
                      <ComboboxOption key={c} value={c} className={optionClass}>
                        {formatSchoolName(c)}
                      </ComboboxOption>
                    ))}
                  </>
                )}
                {otherSchools.length > 0 && (
                  <>
                    <div className={sectionHeaderClass}>All Schools</div>
                    {otherSchools.map((c) => (
                      <ComboboxOption key={c} value={c} className={optionClass}>
                        {formatSchoolName(c)}
                      </ComboboxOption>
                    ))}
                  </>
                )}
                {browseTruncated && (
                  <div className={hintClass}>Type to search for more schools.</div>
                )}
              </>
            ) : otherSchools.length === 0 ? (
              <div className="px-3 py-2 text-sm text-slate-500">No schools found.</div>
            ) : (
              <>
                {otherSchools.map((c) => (
                  <ComboboxOption key={c} value={c} className={optionClass}>
                    {formatSchoolName(c)}
                  </ComboboxOption>
                ))}
                {browseTruncated && (
                  <div className={hintClass}>Type to search for more schools.</div>
                )}
              </>
            )
          ) : filteredCapped.length === 0 ? (
            <div className="px-3 py-2 text-sm text-slate-500">No schools found.</div>
          ) : (
            <>
              {filteredCapped.map((c) => (
                <ComboboxOption key={c} value={c} className={optionClass}>
                  {formatSchoolName(c)}
                </ComboboxOption>
              ))}
              {searchTruncated && (
                <div className={hintClass}>
                  Showing first {MAX_RESULTS} matches. Keep typing to refine.
                </div>
              )}
            </>
          )}
        </ComboboxOptions>
      </div>
    </Combobox>
  )
}
