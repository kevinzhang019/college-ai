import { useState, useMemo, useRef } from 'react'
import { motion, AnimatePresence, Reorder } from 'framer-motion'
import { Combobox, ComboboxInput, ComboboxOption, ComboboxOptions } from '@headlessui/react'
import { useStore } from '../store'
import ExperienceForm from './ExperienceForm'
import CollegeCombobox from './CollegeCombobox'
import type { TestScoreType } from '../types'
import { ALLOWED_MAJORS } from '../types'
import { COUNTRIES, US_STATES } from '../data/locations'
import { formatSchoolName } from '../lib/format'

const TYPE_LABELS: Record<string, string> = {
  extracurricular: 'Extracurricular',
  project: 'Project',
  work: 'Work',
  volunteer: 'Volunteer',
}

const TYPE_COLORS: Record<string, string> = {
  extracurricular: 'bg-teal-500/15 text-teal-400',
  project: 'bg-sky-500/15 text-sky-400',
  work: 'bg-amber-500/15 text-amber-400',
  volunteer: 'bg-purple-500/15 text-purple-400',
}

export default function ExperiencesView() {
  const experiences = useStore((s) => s.experiences)
  const deleteExperience = useStore((s) => s.deleteExperience)
  const profile = useStore((s) => s.profile)
  const setProfileGpa = useStore((s) => s.setProfileGpa)
  const setProfileTestScore = useStore((s) => s.setProfileTestScore)
  const setProfileLocation = useStore((s) => s.setProfileLocation)
  const addPreferredMajor = useStore((s) => s.addPreferredMajor)
  const removePreferredMajor = useStore((s) => s.removePreferredMajor)
  const reorderPreferredMajors = useStore((s) => s.reorderPreferredMajors)
  const savedSchools = useStore((s) => s.profile.savedSchools)
  const addSavedSchool = useStore((s) => s.addSavedSchool)
  const removeSavedSchool = useStore((s) => s.removeSavedSchool)
  const reorderSavedSchools = useStore((s) => s.reorderSavedSchools)
  const [showForm, setShowForm] = useState(false)
  const [majorQuery, setMajorQuery] = useState('')
  const majorInputRef = useRef<HTMLInputElement | null>(null)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [gpaError, setGpaError] = useState('')
  const [scoreError, setScoreError] = useState('')
  const [majorFlash, setMajorFlash] = useState(false)
  const [schoolFlash, setSchoolFlash] = useState(false)

  const validateGpa = (val: string) => {
    if (!val) { setGpaError(''); return }
    const n = parseFloat(val)
    if (isNaN(n) || n < 0 || n > 4.0) setGpaError('0 – 4.0')
    else setGpaError('')
  }

  const validateScore = (val: string, type: TestScoreType) => {
    if (!val) { setScoreError(''); return }
    const n = parseFloat(val)
    if (type === 'sat' && (isNaN(n) || n < 400 || n > 1600)) setScoreError('400 – 1600')
    else if (type === 'act' && (isNaN(n) || n < 1 || n > 36)) setScoreError('1 – 36')
    else setScoreError('')
  }

  const availableMajors = useMemo(() => {
    const selected = new Set(profile.preferredMajors)
    const available = ALLOWED_MAJORS.filter((m) => !selected.has(m))
    if (!majorQuery) return available.slice(0, 50)
    const lower = majorQuery.toLowerCase()
    return available.filter((m) => m.toLowerCase().includes(lower)).slice(0, 50)
  }, [majorQuery, profile.preferredMajors])

  const editingExp = editingId
    ? experiences.find((e) => e.id === editingId) || null
    : null

  return (
    <div className="flex-1 overflow-y-auto custom-scrollbar px-4 py-6">
      <div className="max-w-2xl mx-auto">
        <div className="mb-6">
          <h2 className="text-lg font-semibold text-slate-100">
            Your Profile
          </h2>
          <p className="text-sm text-slate-500 mt-0.5">
            These are automatically included as context in Essay mode.
          </p>
        </div>

        {/* Academic Info card */}
        <div className="card p-4 mb-6">
          <h3 className="text-sm font-medium text-slate-100 mb-1">Academic Info</h3>
          <p className="text-xs text-slate-500 mb-3">Auto-populates your admissions estimates.</p>

          <div className="flex gap-3 items-start">
            {/* GPA */}
            <div className="w-28">
              <label className="block text-xs font-medium text-slate-400 mb-1">GPA</label>
              <input
                type="number"
                step="0.01"
                min="0"
                max="4"
                value={profile.gpa}
                onChange={(e) => setProfileGpa(e.target.value)}
                onBlur={(e) => validateGpa(e.target.value)}
                placeholder="e.g. 3.8"
                className={`input-field-compact text-sm ${gpaError ? 'border-red-500/60 focus:ring-red-500/40 focus:border-red-500' : ''}`}
              />
              {gpaError && <p className="text-[10px] text-red-400 mt-0.5">{gpaError}</p>}
            </div>

            {/* Test type toggle */}
            <div>
              <label className="block text-xs font-medium text-slate-400 mb-1">Test</label>
              <div className="flex rounded-lg overflow-hidden border border-dark-700">
                <button
                  type="button"
                  onClick={() => {
                    setProfileTestScore('sat', '')
                    setScoreError('')
                  }}
                  className={`px-3 py-2 text-xs font-medium transition-colors ${
                    profile.testScoreType === 'sat'
                      ? 'bg-forest-600/20 text-forest-300'
                      : 'bg-dark-800 text-slate-400 hover:text-slate-200'
                  }`}
                >
                  SAT
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setProfileTestScore('act', '')
                    setScoreError('')
                  }}
                  className={`px-3 py-2 text-xs font-medium transition-colors ${
                    profile.testScoreType === 'act'
                      ? 'bg-forest-600/20 text-forest-300'
                      : 'bg-dark-800 text-slate-400 hover:text-slate-200'
                  }`}
                >
                  ACT
                </button>
              </div>
            </div>

            {/* Test score */}
            <div className="w-28">
              <label className="block text-xs font-medium text-slate-400 mb-1">
                {profile.testScoreType === 'sat' ? 'SAT Score' : 'ACT Score'}
              </label>
              <input
                type="number"
                value={profile.testScore}
                onChange={(e) => setProfileTestScore(profile.testScoreType, e.target.value)}
                onBlur={(e) => validateScore(e.target.value, profile.testScoreType)}
                placeholder={profile.testScoreType === 'sat' ? '400 – 1600' : '1 – 36'}
                className={`input-field-compact text-sm ${scoreError ? 'border-red-500/60 focus:ring-red-500/40 focus:border-red-500' : ''}`}
              />
              {scoreError && <p className="text-[10px] text-red-400 mt-0.5">{scoreError}</p>}
            </div>
          </div>

          <label className="block text-xs font-medium text-slate-400 mb-1 mt-4">Location</label>
          <div className="flex gap-3">
            <div className={profile.country === 'US' ? 'w-1/2' : 'flex-1'}>
              <select
                value={profile.country}
                onChange={(e) => {
                  const c = e.target.value
                  const label = COUNTRIES.find((x) => x.value === c)?.label || c
                  setProfileLocation(c, label, c === 'US' ? profile.state : '')
                }}
                className="input-field-compact text-sm w-full"
              >
                <option value="">Select country...</option>
                {COUNTRIES.map((c) => (
                  <option key={c.value} value={c.value}>{c.label}</option>
                ))}
              </select>
            </div>
            {profile.country === 'US' && (
              <div className="w-1/2">
                <select
                  value={profile.state}
                  onChange={(e) => setProfileLocation(profile.country, profile.countryLabel, e.target.value)}
                  className="input-field-compact text-sm w-full"
                >
                  <option value="">Select state...</option>
                  {US_STATES.map((s) => (
                    <option key={s.value} value={s.value}>{s.label}</option>
                  ))}
                </select>
              </div>
            )}
          </div>
        </div>

        {/* Major Preferences card */}
        <div className={`card p-4 mb-6 transition-shadow duration-500 ${majorFlash ? 'shadow-[0_0_16px_rgba(239,68,68,0.5)]' : ''}`}>
          <h3 className="text-sm font-medium text-slate-100 mb-1">
            Major Preferences
            <span className="text-slate-500 font-normal ml-1.5">({profile.preferredMajors.length}/15)</span>
          </h3>
          <p className="text-xs text-slate-500 mb-3">
            Rank your preferred majors. Cole will use this to personalize program and admissions advice.
          </p>

          <Combobox
            value={null}
            onChange={(val: string | null) => {
              if (val) {
                if (profile.preferredMajors.length >= 15) {
                  setMajorFlash(true)
                  setTimeout(() => setMajorFlash(false), 600)
                  return
                }
                addPreferredMajor(val)
                setMajorQuery('')
                majorInputRef.current?.blur()
                setTimeout(() => majorInputRef.current?.focus(), 100)
              }
            }}
            onClose={() => setMajorQuery('')}
            immediate
          >
            <div className="relative">
              <ComboboxInput
                ref={majorInputRef}
                className="input-field-compact text-sm w-full"
                placeholder="Search and add a major..."
                displayValue={() => ''}
                onChange={(e) => setMajorQuery(e.target.value)}
                value={majorQuery}
              />
              <ComboboxOptions className="absolute z-50 mt-1 max-h-48 w-full overflow-auto rounded-xl bg-dark-900 shadow-dark-lg border border-dark-700 py-1">
                {availableMajors.map((m) => (
                  <ComboboxOption
                    key={m}
                    value={m}
                    className="px-4 py-2 text-sm text-slate-300 cursor-pointer data-[focus]:bg-dark-800"
                  >
                    {m}
                  </ComboboxOption>
                ))}
                {availableMajors.length === 0 && (
                  <div className="px-4 py-2 text-sm text-slate-500">No majors found</div>
                )}
              </ComboboxOptions>
            </div>
          </Combobox>

          {profile.preferredMajors.length > 0 && (
            <Reorder.Group
              axis="y"
              values={profile.preferredMajors}
              onReorder={reorderPreferredMajors}
              className="mt-3 space-y-1.5"
            >
              <AnimatePresence>
                {profile.preferredMajors.map((major, i) => (
                  <Reorder.Item
                    key={major}
                    value={major}
                    initial={{ opacity: 0, y: -4 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, x: -20 }}
                    className="flex items-center gap-2 bg-dark-800 rounded-lg px-3 py-2 cursor-grab active:cursor-grabbing"
                  >
                    <span className="text-xs font-medium text-slate-500 w-5 shrink-0">#{i + 1}</span>
                    {/* Drag handle */}
                    <svg className="w-3.5 h-3.5 text-slate-600 shrink-0" fill="currentColor" viewBox="0 0 24 24">
                      <circle cx="9" cy="6" r="1.5" /><circle cx="15" cy="6" r="1.5" />
                      <circle cx="9" cy="12" r="1.5" /><circle cx="15" cy="12" r="1.5" />
                      <circle cx="9" cy="18" r="1.5" /><circle cx="15" cy="18" r="1.5" />
                    </svg>
                    <span className="text-sm text-slate-200 flex-1">{major}</span>
                    <button
                      onClick={() => removePreferredMajor(major)}
                      className="p-0.5 text-slate-500 hover:text-red-400 transition-colors shrink-0"
                    >
                      <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                      </svg>
                    </button>
                  </Reorder.Item>
                ))}
              </AnimatePresence>
            </Reorder.Group>
          )}
        </div>

        {/* Saved Schools card */}
        <div className={`card p-4 mb-6 transition-shadow duration-500 ${schoolFlash ? 'shadow-[0_0_16px_rgba(239,68,68,0.5)]' : ''}`}>
          <h3 className="text-sm font-medium text-slate-100 mb-1">
            Saved Schools
            <span className="text-slate-500 font-normal ml-1.5">({savedSchools.length}/25)</span>
          </h3>
          <p className="text-xs text-slate-500 mb-3">
            Rank schools by preference. Drag to reorder. They'll appear at the top of school dropdowns.
          </p>

          <CollegeCombobox
            value={null}
            onChange={(val) => {
              if (val) {
                if (savedSchools.length >= 25) {
                  setSchoolFlash(true)
                  setTimeout(() => setSchoolFlash(false), 600)
                  return
                }
                addSavedSchool(val)
              }
            }}
            showDefaultScreen={false}
            reopenOnSelect
            placeholder="Select a school"
            excludeValues={savedSchools}
          />

          {savedSchools.length > 0 && (
            <Reorder.Group
              axis="y"
              values={savedSchools}
              onReorder={reorderSavedSchools}
              className="mt-3 space-y-1.5"
            >
              <AnimatePresence>
                {savedSchools.map((school, i) => (
                  <Reorder.Item
                    key={school}
                    value={school}
                    initial={{ opacity: 0, y: -4 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, x: -20 }}
                    className="flex items-center gap-2 bg-dark-800 rounded-lg px-3 py-2 cursor-grab active:cursor-grabbing"
                  >
                    <span className="text-xs font-medium text-slate-500 w-5 shrink-0">#{i + 1}</span>
                    <svg className="w-3.5 h-3.5 text-slate-600 shrink-0" fill="currentColor" viewBox="0 0 24 24">
                      <circle cx="9" cy="6" r="1.5" /><circle cx="15" cy="6" r="1.5" />
                      <circle cx="9" cy="12" r="1.5" /><circle cx="15" cy="12" r="1.5" />
                      <circle cx="9" cy="18" r="1.5" /><circle cx="15" cy="18" r="1.5" />
                    </svg>
                    <span className="text-sm text-slate-200 flex-1 truncate">{formatSchoolName(school)}</span>
                    <button
                      onClick={() => removeSavedSchool(school)}
                      className="p-0.5 text-slate-500 hover:text-red-400 transition-colors shrink-0"
                    >
                      <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                      </svg>
                    </button>
                  </Reorder.Item>
                ))}
              </AnimatePresence>
            </Reorder.Group>
          )}
        </div>

        {/* Experiences card */}
        <div className="card p-4 mb-6">
          <div className="flex items-center justify-between mb-1">
            <h3 className="text-sm font-medium text-slate-100">Experiences</h3>
            <button
              onClick={() => {
                setEditingId(null)
                setShowForm(true)
              }}
              className="btn-primary text-xs px-3 py-1.5"
            >
              <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
              </svg>
              Add
            </button>
          </div>
          <p className="text-xs text-slate-500 mb-3">
            Extracurriculars, projects, work, and volunteer activities. Used to personalize essay brainstorming.
          </p>

          {experiences.length === 0 && !showForm && (
            <motion.div
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              className="text-center py-8"
            >
              <span className="text-4xl mb-3 block">📋</span>
              <p className="text-sm text-slate-500">
                No experiences yet
              </p>
            </motion.div>
          )}

          {experiences.length > 0 && (
            <div className="space-y-2">
              <AnimatePresence>
                {experiences.map((exp) => (
                  <motion.div
                    key={exp.id}
                    layout
                    initial={{ opacity: 0, y: 8 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, scale: 0.95 }}
                    className="bg-dark-800 rounded-lg p-3 group"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 mb-1">
                          <h3 className="text-sm font-medium text-slate-100 truncate">
                            {exp.title}
                          </h3>
                          <span
                            className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                              TYPE_COLORS[exp.type] || 'bg-slate-500/15 text-slate-400'
                            }`}
                          >
                            {TYPE_LABELS[exp.type] || exp.type}
                          </span>
                        </div>
                        {exp.organization && (
                          <p className="text-xs text-slate-400">{exp.organization}</p>
                        )}
                        {(exp.startDate || exp.endDate) && (
                          <p className="text-xs text-slate-500 mt-0.5">
                            {exp.startDate}
                            {exp.endDate ? ` – ${exp.endDate}` : ''}
                          </p>
                        )}
                        {exp.description && (
                          <p className="text-xs text-slate-400 mt-2 leading-relaxed line-clamp-2">
                            {exp.description}
                          </p>
                        )}
                      </div>
                      <div className="flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity shrink-0">
                        <button
                          onClick={() => {
                            setEditingId(exp.id)
                            setShowForm(true)
                          }}
                          className="p-1.5 text-slate-500 hover:text-slate-200 transition-colors"
                        >
                          <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" />
                          </svg>
                        </button>
                        <button
                          onClick={() => deleteExperience(exp.id)}
                          className="p-1.5 text-slate-500 hover:text-red-400 transition-colors"
                        >
                          <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                          </svg>
                        </button>
                      </div>
                    </div>
                  </motion.div>
                ))}
              </AnimatePresence>
            </div>
          )}
        </div>

        {/* Form modal */}
        <AnimatePresence>
          {showForm && (
            <ExperienceForm
              experience={editingExp}
              onClose={() => {
                setShowForm(false)
                setEditingId(null)
              }}
            />
          )}
        </AnimatePresence>
      </div>
    </div>
  )
}
