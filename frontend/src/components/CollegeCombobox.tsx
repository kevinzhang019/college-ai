import { useState, useMemo, useRef, useCallback } from 'react'
import { Combobox, ComboboxInput, ComboboxOption, ComboboxOptions, ComboboxButton } from '@headlessui/react'
import { useStore } from '../store'
import { formatSchoolName } from '../lib/format'

interface Props {
  value: string | null
  onChange: (value: string | null) => void
  compact?: boolean
  showDefaultScreen?: boolean
  placeholder?: string
  reopenOnSelect?: boolean
  excludeValues?: string[]
}

export default function CollegeCombobox({ value, onChange, compact, placeholder = 'Select a school (optional)', reopenOnSelect, excludeValues }: Props) {
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

  const validSavedSchools = useMemo(
    () => savedSchools.filter((s) => options.includes(s) && (!excluded || !excluded.has(s))),
    [savedSchools, options, excluded],
  )

  const virtualOptions = useMemo<string[]>(() => {
    const base = excluded ? options.filter((c) => !excluded.has(c)) : options
    if (query) {
      const lower = query.toLowerCase()
      return base.filter((c) => c.toLowerCase().includes(lower))
    }
    const savedSet = new Set(validSavedSchools)
    const others = base.filter((c) => !savedSet.has(c))
    return [...validSavedSchools, ...others]
  }, [query, options, excluded, validSavedSchools])

  return (
    <Combobox
      value={value}
      onChange={handleChange}
      onClose={() => setQuery('')}
      immediate
      virtual={{ options: virtualOptions }}
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
          {({ option }: { option: string }) => (
            <ComboboxOption
              value={option}
              className="px-4 py-2 text-sm text-slate-300 cursor-pointer data-[focus]:bg-dark-800 data-[selected]:text-forest-400 data-[selected]:font-medium"
            >
              {formatSchoolName(option)}
            </ComboboxOption>
          )}
        </ComboboxOptions>
      </div>
    </Combobox>
  )
}
