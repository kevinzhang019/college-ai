import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useStore } from '../store'
import { predict } from '../api'
import CollegeCombobox from './CollegeCombobox'
import MajorCombobox from './MajorCombobox'
import type { ProfileData, PredictionResult, Residency, SelectedSchool, TestScoreType } from '../types'

type Phase = 'idle' | 'loading' | 'done'

const PROB_COLORS: Record<string, string> = {
  safety: 'text-emerald-400',
  match: 'text-amber-400',
  reach: 'text-rose-400',
}

const CLASS_STYLES: Record<string, string> = {
  safety: 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/30',
  match: 'bg-amber-500/15 text-amber-400 border border-amber-500/30',
  reach: 'bg-rose-500/15 text-rose-400 border border-rose-500/30',
}

const CLASS_LABELS: Record<string, string> = {
  safety: 'Safety',
  match: 'Match',
  reach: 'Reach',
}

const MAX_SCHOOLS = 10

const filterNumericInput = (value: string, allowDot: boolean): string => {
  if (allowDot) {
    return value.replace(/[^0-9.]/g, '').replace(/(\..*)\./g, '$1')
  }
  return value.replace(/[^0-9]/g, '')
}

function computeResidency(
  schoolName: string,
  profile: ProfileData,
  schoolStates: Record<string, string>,
): Residency | null {
  if (!profile.country) return null
  if (profile.country !== 'US') return 'international'
  if (!profile.state) return null
  const schoolState = schoolStates[schoolName]
  if (!schoolState) return null
  return schoolState === profile.state ? 'inState' : 'outOfState'
}

export default function AdmissionsView() {
  const profile = useStore((s) => s.profile)
  const schoolStates = useStore((s) => s.schoolStates)
  const collegeOptions = useStore((s) => s.collegeOptions)

  // Local copies of stats — imported from profile on mount, independent afterward
  const [gpa, setGpa] = useState(profile.gpa)
  const [testScoreType, setTestScoreType] = useState<TestScoreType>(profile.testScoreType)
  const [testScore, setTestScore] = useState(profile.testScore)

  const locationEligible = Boolean(
    profile.country && (profile.country !== 'US' || profile.state)
  )

  // Local form state
  const [selectedSchools, setSelectedSchools] = useState<SelectedSchool[]>([])
  const [defaultMajor, setDefaultMajor] = useState<string | null>(null)
  const [defaultResidency, setDefaultResidency] = useState<Residency | 'useLocation' | null>(
    locationEligible ? 'useLocation' : null
  )

  // Validation
  const [gpaError, setGpaError] = useState('')
  const [scoreError, setScoreError] = useState('')

  // Results
  const [phase, setPhase] = useState<Phase>('idle')
  const [schoolResults, setSchoolResults] = useState<Record<string, PredictionResult>>({})
  const [error, setError] = useState<string | null>(null)

  const validateGpa = (val: string) => {
    if (!val) { setGpaError('Required'); return false }
    const n = parseFloat(val)
    if (isNaN(n) || n < 0 || n > 5.0) { setGpaError('0 – 5.0'); return false }
    setGpaError('')
    return true
  }

  const validateScore = (val: string, type: TestScoreType) => {
    if (!val) { setScoreError('Required'); return false }
    const n = parseFloat(val)
    if (type === 'sat' && (isNaN(n) || n < 400 || n > 1600)) { setScoreError('400 – 1600'); return false }
    if (type === 'act' && (isNaN(n) || n < 1 || n > 36)) { setScoreError('1 – 36'); return false }
    setScoreError('')
    return true
  }

  const handleAddSchool = (school: string | null) => {
    if (!school || selectedSchools.some((s) => s.name === school) || selectedSchools.length >= MAX_SCHOOLS) return
    const residency = defaultResidency === 'useLocation'
      ? computeResidency(school, profile, schoolStates)
      : defaultResidency
    setSelectedSchools((prev) => [...prev, {
      name: school,
      residency,
      major: defaultMajor,
    }])
  }

  const savedSchoolsToAdd = profile.savedSchools.filter(
    (s) => collegeOptions.includes(s) && !selectedSchools.some((sel) => sel.name === s)
  )

  const handleAddSavedSchools = () => {
    const slotsLeft = MAX_SCHOOLS - selectedSchools.length
    const toAdd = savedSchoolsToAdd.slice(0, slotsLeft)
    const newSchools: SelectedSchool[] = toAdd.map((name) => ({
      name,
      residency: defaultResidency === 'useLocation'
        ? computeResidency(name, profile, schoolStates)
        : defaultResidency,
      major: defaultMajor,
    }))
    setSelectedSchools((prev) => [...prev, ...newSchools])
  }

  const handleRemoveSchool = (schoolName: string) => {
    setSelectedSchools((prev) => prev.filter((s) => s.name !== schoolName))
  }

  const handleSchoolMajor = (schoolName: string, major: string | null) => {
    setSelectedSchools((prev) => prev.map((s) =>
      s.name === schoolName ? { ...s, major } : s
    ))
  }

  const handleSchoolResidency = (schoolName: string, residency: Residency | null) => {
    setSelectedSchools((prev) => prev.map((s) =>
      s.name === schoolName ? { ...s, residency } : s
    ))
  }

  const canSubmit =
    gpa && !gpaError &&
    testScore && !scoreError &&
    selectedSchools.length > 0 &&
    phase === 'idle'

  const handleSubmit = async () => {
    const gpaValid = validateGpa(gpa)
    const scoreValid = validateScore(testScore, testScoreType)
    if (!gpaValid || !scoreValid || selectedSchools.length === 0) return

    setPhase('loading')
    setError(null)
    setSchoolResults({})

    try {
      const baseParams = {
        gpa: parseFloat(gpa),
        ...(testScoreType === 'sat'
          ? { sat: parseFloat(testScore) }
          : { act: parseFloat(testScore) }),
      }

      const promises = selectedSchools.map(async (school) => {
        try {
          const result = await predict({
            ...baseParams,
            school_name: school.name,
            ...(school.residency ? { residency: school.residency } : {}),
            ...(school.major ? { major: school.major } : {}),
          })
          setSchoolResults((prev) => ({ ...prev, [school.name]: result }))
        } catch (err) {
          setSchoolResults((prev) => ({
            ...prev,
            [school.name]: {
              school_name: school.name,
              error: err instanceof Error ? err.message : 'Failed',
              probability: 0,
              confidence_interval: [0, 0] as [number, number],
              classification: 'reach' as const,
              school_acceptance_rate: 0,
              factors: [],
            },
          }))
        }
      })

      await Promise.all(promises)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Something went wrong')
    } finally {
      setPhase('done')
    }
  }

  const handleClearSelections = () => {
    setSelectedSchools([])
    setSchoolResults({})
    setPhase('idle')
    setError(null)
  }

  const handleCalculateAgain = () => {
    setSchoolResults({})
    setPhase('idle')
    setError(null)
  }

  return (
    <div className="flex-1 overflow-y-auto custom-scrollbar px-4 py-6">
      <div className="max-w-2xl mx-auto">
        {/* Header */}
        <div className="mb-6">
          <h2 className="text-lg font-semibold text-slate-100">Admissions Calculator</h2>
          <p className="text-sm text-slate-500 mt-0.5">
            Estimate your admission chances
          </p>
        </div>

        {/* Stats card */}
        <div className="card p-4 mb-4 space-y-3">
          {/* Row 1: GPA + test type + test score */}
          <div className="flex gap-3 items-start">
            <div className="w-28">
              <label className="block text-xs font-medium text-slate-400 mb-1">GPA *</label>
              <input
                type="text"
                inputMode="decimal"
                value={gpa}
                onChange={(e) => { setGpa(filterNumericInput(e.target.value, true)); setGpaError('') }}
                onBlur={(e) => validateGpa(e.target.value)}
                placeholder="e.g. 3.8"
                className={`input-field-compact text-sm ${gpaError ? 'border-red-500/60 focus:ring-red-500/40 focus:border-red-500' : ''}`}
              />
              {gpaError && <p className="text-[10px] text-red-400 mt-0.5">{gpaError}</p>}
            </div>

            <div>
              <label className="block text-xs font-medium text-slate-400 mb-1">Test *</label>
              <div className="flex rounded-lg overflow-hidden border border-dark-700">
                <button
                  type="button"
                  onClick={() => { setTestScoreType('sat'); setTestScore(''); setScoreError('') }}
                  className={`px-3 py-2 text-xs font-medium transition-colors ${
                    testScoreType === 'sat'
                      ? 'bg-forest-600/20 text-forest-300'
                      : 'bg-dark-800 text-slate-400 hover:text-slate-200'
                  }`}
                >
                  SAT
                </button>
                <button
                  type="button"
                  onClick={() => { setTestScoreType('act'); setTestScore(''); setScoreError('') }}
                  className={`px-3 py-2 text-xs font-medium transition-colors ${
                    testScoreType === 'act'
                      ? 'bg-forest-600/20 text-forest-300'
                      : 'bg-dark-800 text-slate-400 hover:text-slate-200'
                  }`}
                >
                  ACT
                </button>
              </div>
            </div>

            <div className="w-28">
              <label className="block text-xs font-medium text-slate-400 mb-1">
                {testScoreType === 'sat' ? 'SAT Score *' : 'ACT Score *'}
              </label>
              <input
                type="text"
                inputMode="numeric"
                value={testScore}
                onChange={(e) => { setTestScore(filterNumericInput(e.target.value, false)); setScoreError('') }}
                onBlur={(e) => validateScore(e.target.value, testScoreType)}
                placeholder={testScoreType === 'sat' ? '400 – 1600' : '1 – 36'}
                className={`input-field-compact text-sm ${scoreError ? 'border-red-500/60 focus:ring-red-500/40 focus:border-red-500' : ''}`}
              />
              {scoreError && <p className="text-[10px] text-red-400 mt-0.5">{scoreError}</p>}
            </div>

            <div className="flex-1">
              <label className="block text-xs font-medium text-slate-400 mb-1">Default Residency</label>
              <select
                value={defaultResidency || ''}
                onChange={(e) => setDefaultResidency((e.target.value || null) as Residency | 'useLocation' | null)}
                className="input-field-compact text-sm"
              >
                {locationEligible && <option value="useLocation">Use Location</option>}
                <option value="">Not specified</option>
                <option value="inState">In-State</option>
                <option value="outOfState">Out-of-State</option>
                <option value="international">International</option>
              </select>
            </div>
          </div>

          {/* Row 2: Default Major */}
          <div>
            <label className="block text-xs font-medium text-slate-400 mb-1">Default Major</label>
            <MajorCombobox value={defaultMajor} onChange={setDefaultMajor} />
          </div>
        </div>

        {/* School picker */}
        <div className="card p-4 mb-4">
          <div className="flex items-center justify-between mb-2">
            <label className="text-xs font-medium text-slate-400">
              Schools ({selectedSchools.length}/{MAX_SCHOOLS})
            </label>
            {phase === 'idle' && savedSchoolsToAdd.length > 0 && (
              <button
                onClick={handleAddSavedSchools}
                className="text-xs text-forest-400 hover:text-forest-300 border border-transparent hover:border-forest-600/40 rounded-lg px-2 py-0.5 transition-all"
              >
                Add saved schools
              </button>
            )}
          </div>
          {phase === 'idle' && (
            selectedSchools.length < MAX_SCHOOLS ? (
              <CollegeCombobox
                value={null}
                onChange={handleAddSchool}
                compact
                reopenOnSelect
                placeholder="Select a school"
                excludeValues={selectedSchools.map((s) => s.name)}
              />
            ) : (
              <p className="text-xs text-slate-500 py-2">Maximum {MAX_SCHOOLS} schools reached.</p>
            )
          )}

          {/* Selected school cards */}
          {selectedSchools.length > 0 && (
            <div className={`${phase === 'idle' ? 'mt-3' : ''} rounded-xl border border-dark-700 overflow-hidden`}>
              <AnimatePresence initial={false}>
                {selectedSchools.map((school, i) => {
                  const result = schoolResults[school.name]
                  const pct = result && !result.error ? Math.round(result.probability * 100) : null

                  return (
                    <motion.div
                      key={school.name}
                      layout
                      initial={{ opacity: 0, y: -4 }}
                      animate={{ opacity: 1, y: 0 }}
                      exit={{ opacity: 0, x: -20 }}
                      transition={{ duration: 0.2 }}
                      className={`px-3 py-2.5 flex items-center gap-2 ${
                        i % 2 === 0 ? 'bg-dark-800' : 'bg-dark-800/50'
                      } ${i > 0 ? 'border-t border-dark-700' : ''}`}
                    >
                      <span
                        className="text-sm text-slate-200 font-medium flex-1 min-w-0 truncate"
                        title={school.name}
                      >
                        {school.name}
                      </span>

                      {phase === 'idle' ? (
                        <>
                          <div className="flex-shrink-0">
                            <MajorCombobox
                              value={school.major}
                              onChange={(m) => handleSchoolMajor(school.name, m)}
                              compact
                            />
                          </div>

                          <select
                            value={school.residency || ''}
                            onChange={(e) => handleSchoolResidency(school.name, (e.target.value || null) as Residency | null)}
                            className="input-field-compact text-xs py-1.5 w-28 flex-shrink-0"
                          >
                            <option value="">No residency</option>
                            <option value="inState">In-State</option>
                            <option value="outOfState">Out-of-State</option>
                            <option value="international">International</option>
                          </select>

                          <button
                            onClick={() => handleRemoveSchool(school.name)}
                            className="text-slate-500 hover:text-red-400 transition-colors flex-shrink-0"
                          >
                            <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                            </svg>
                          </button>
                        </>
                      ) : result ? (
                        <div className="flex items-center gap-2 flex-shrink-0">
                          {result.error ? (
                            <span className="text-xs text-red-400">{result.error}</span>
                          ) : (
                            <>
                              <span className={`text-lg font-bold ${PROB_COLORS[result.classification] || 'text-slate-100'}`}>
                                {pct}%
                              </span>
                              <span className={`text-[10px] font-semibold px-2 py-0.5 rounded-full ${CLASS_STYLES[result.classification] || ''}`}>
                                {CLASS_LABELS[result.classification] || result.classification}
                              </span>
                            </>
                          )}
                        </div>
                      ) : (
                        <div className="flex gap-2 flex-shrink-0">
                          <div className="h-5 w-16 bg-dark-700 rounded-lg animate-pulse" />
                          <div className="h-5 w-12 bg-dark-700 rounded-full animate-pulse [animation-delay:0.15s]" />
                        </div>
                      )}
                    </motion.div>
                  )
                })}
              </AnimatePresence>
            </div>
          )}
        </div>

        {/* Buttons */}
        {phase === 'done' ? (
          <div className="flex gap-3 mb-6">
            <button
              onClick={handleClearSelections}
              className="flex-1 text-sm py-2.5 rounded-xl border border-dark-600 text-slate-300 hover:bg-dark-700 transition-colors"
            >
              Clear Selections
            </button>
            <button
              onClick={handleCalculateAgain}
              className="btn-primary flex-1 text-sm"
            >
              Calculate Again
            </button>
          </div>
        ) : (
          <button
            onClick={handleSubmit}
            disabled={!canSubmit}
            className="btn-primary w-full text-sm mb-6"
          >
            {phase === 'loading' ? (
              <span className="flex items-center gap-2">
                <span className="flex gap-1">
                  <span className="w-1.5 h-1.5 bg-white rounded-full dot-bounce" />
                  <span className="w-1.5 h-1.5 bg-white rounded-full dot-bounce" />
                  <span className="w-1.5 h-1.5 bg-white rounded-full dot-bounce" />
                </span>
                Calculating...
              </span>
            ) : (
              'Calculate Chances'
            )}
          </button>
        )}

        {/* Error */}
        {error && (
          <motion.div
            initial={{ opacity: 0, y: 4 }}
            animate={{ opacity: 1, y: 0 }}
            className="card p-3 mb-4 border-red-500/30"
          >
            <p className="text-sm text-red-400">{error}</p>
          </motion.div>
        )}
      </div>
    </div>
  )
}
