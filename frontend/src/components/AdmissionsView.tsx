import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useStore } from '../store'
import { predict } from '../api'
import CollegeCombobox from './CollegeCombobox'
import MajorCombobox from './MajorCombobox'
import PredictionCard from './PredictionCard'
import type { ProfileData, PredictionResult, Residency, SelectedSchool, TestScoreType } from '../types'

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
  if (profile.country !== 'US') return 'outOfState'
  if (!profile.state) return null
  const schoolState = schoolStates[schoolName]
  if (!schoolState) return null
  return schoolState === profile.state ? 'inState' : 'outOfState'
}

export default function AdmissionsView() {
  const profile = useStore((s) => s.profile)
  const schoolStates = useStore((s) => s.schoolStates)
  const setProfileGpa = useStore((s) => s.setProfileGpa)
  const setProfileTestScore = useStore((s) => s.setProfileTestScore)

  const hasProfileLocation = Boolean(profile.country)

  // Local form state
  const [selectedSchools, setSelectedSchools] = useState<SelectedSchool[]>([])
  const [defaultMajor, setDefaultMajor] = useState<string | null>(null)
  const [defaultResidency, setDefaultResidency] = useState<Residency | null>(null)

  // Validation
  const [gpaError, setGpaError] = useState('')
  const [scoreError, setScoreError] = useState('')

  // Results
  const [results, setResults] = useState<PredictionResult[] | null>(null)
  const [loading, setLoading] = useState(false)
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
    const residency = hasProfileLocation
      ? computeResidency(school, profile, schoolStates)
      : defaultResidency
    setSelectedSchools((prev) => [...prev, {
      name: school,
      residency,
      major: defaultMajor,
    }])
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
    profile.gpa && !gpaError &&
    profile.testScore && !scoreError &&
    selectedSchools.length > 0 &&
    !loading

  const handleSubmit = async () => {
    const gpaValid = validateGpa(profile.gpa)
    const scoreValid = validateScore(profile.testScore, profile.testScoreType)
    if (!gpaValid || !scoreValid || selectedSchools.length === 0) return

    setLoading(true)
    setError(null)
    setResults(null)

    try {
      const baseParams = {
        gpa: parseFloat(profile.gpa),
        ...(profile.testScoreType === 'sat'
          ? { sat: parseFloat(profile.testScore) }
          : { act: parseFloat(profile.testScore) }),
      }

      const promises = selectedSchools.map((school) => {
        const residency = hasProfileLocation
          ? computeResidency(school.name, profile, schoolStates)
          : school.residency
        return predict({
          ...baseParams,
          school_name: school.name,
          ...(residency ? { residency } : {}),
          ...(school.major ? { major: school.major } : {}),
        }).catch((err) => ({
          school_name: school.name,
          error: err instanceof Error ? err.message : 'Failed',
          probability: 0,
          confidence_interval: [0, 0] as [number, number],
          classification: 'reach' as const,
          school_acceptance_rate: 0,
          factors: [],
        }))
      })

      const allResults = await Promise.all(promises)
      allResults.sort((a, b) => b.probability - a.probability)
      setResults(allResults)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Something went wrong')
    } finally {
      setLoading(false)
    }
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
                value={profile.gpa}
                onChange={(e) => { setProfileGpa(filterNumericInput(e.target.value, true)); setGpaError('') }}
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
                  onClick={() => { setProfileTestScore('sat', ''); setScoreError('') }}
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
                  onClick={() => { setProfileTestScore('act', ''); setScoreError('') }}
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

            <div className="flex-1">
              <label className="block text-xs font-medium text-slate-400 mb-1">
                {profile.testScoreType === 'sat' ? 'SAT Score *' : 'ACT Score *'}
              </label>
              <input
                type="text"
                inputMode="numeric"
                value={profile.testScore}
                onChange={(e) => { setProfileTestScore(profile.testScoreType, filterNumericInput(e.target.value, false)); setScoreError('') }}
                onBlur={(e) => validateScore(e.target.value, profile.testScoreType)}
                placeholder={profile.testScoreType === 'sat' ? '400 – 1600' : '1 – 36'}
                className={`input-field-compact text-sm ${scoreError ? 'border-red-500/60 focus:ring-red-500/40 focus:border-red-500' : ''}`}
              />
              {scoreError && <p className="text-[10px] text-red-400 mt-0.5">{scoreError}</p>}
            </div>
          </div>

          {/* Row 2: Default Major + Default Residency */}
          <div className="flex gap-3">
            <div className="flex-1">
              <label className="block text-xs font-medium text-slate-400 mb-1">Default Major</label>
              <MajorCombobox value={defaultMajor} onChange={setDefaultMajor} />
            </div>
            <div className="w-40">
              <label className="block text-xs font-medium text-slate-400 mb-1">Default Residency</label>
              {hasProfileLocation ? (
                <select
                  disabled
                  value=""
                  className="input-field-compact text-sm opacity-50 cursor-not-allowed"
                >
                  <option value="">Use Location</option>
                </select>
              ) : (
                <select
                  value={defaultResidency || ''}
                  onChange={(e) => setDefaultResidency((e.target.value || null) as Residency | null)}
                  className="input-field-compact text-sm"
                >
                  <option value="">Not specified</option>
                  <option value="inState">In-State</option>
                  <option value="outOfState">Out-of-State</option>
                  <option value="outOfState">International</option>
                </select>
              )}
            </div>
          </div>
        </div>

        {/* School picker */}
        <div className="card p-4 mb-4">
          <label className="block text-xs font-medium text-slate-400 mb-2">
            Schools ({selectedSchools.length}/{MAX_SCHOOLS})
          </label>
          {selectedSchools.length < MAX_SCHOOLS ? (
            <CollegeCombobox
              value={null}
              onChange={handleAddSchool}
              compact
            />
          ) : (
            <p className="text-xs text-slate-500 py-2">Maximum {MAX_SCHOOLS} schools reached.</p>
          )}

          {/* Selected school cards — vertical list with alternating greys */}
          {selectedSchools.length > 0 && (
            <div className="mt-3 rounded-xl border border-dark-700">
              {selectedSchools.map((school, i) => (
                <div
                  key={school.name}
                  className={`px-3 py-2.5 flex items-center gap-2 ${
                    i % 2 === 0 ? 'bg-dark-800' : 'bg-dark-800/50'
                  } ${i > 0 ? 'border-t border-dark-700' : ''}${
                    i === 0 ? ' rounded-t-xl' : ''
                  }${i === selectedSchools.length - 1 ? ' rounded-b-xl' : ''}`}
                >
                  <span
                    className="text-sm text-slate-200 font-medium flex-1 min-w-0 truncate"
                    title={school.name}
                  >
                    {school.name}
                  </span>

                  <div className="flex-shrink-0">
                    <MajorCombobox
                      value={school.major}
                      onChange={(m) => handleSchoolMajor(school.name, m)}
                      compact
                    />
                  </div>

                  {hasProfileLocation ? (
                    <select
                      disabled
                      value={computeResidency(school.name, profile, schoolStates) || ''}
                      className="input-field-compact text-xs py-1.5 w-28 flex-shrink-0 opacity-50 cursor-not-allowed"
                    >
                      <option value="">Unknown</option>
                      <option value="inState">In-State</option>
                      <option value="outOfState">{profile.country !== 'US' ? 'International' : 'Out-of-State'}</option>
                    </select>
                  ) : (
                    <select
                      value={school.residency || ''}
                      onChange={(e) => handleSchoolResidency(school.name, (e.target.value || null) as Residency | null)}
                      className="input-field-compact text-xs py-1.5 w-28 flex-shrink-0"
                    >
                      <option value="">No residency</option>
                      <option value="inState">In-State</option>
                      <option value="outOfState">Out-of-State</option>
                      <option value="outOfState">International</option>
                    </select>
                  )}

                  <button
                    onClick={() => handleRemoveSchool(school.name)}
                    className="text-slate-500 hover:text-red-400 transition-colors flex-shrink-0"
                  >
                    <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                    </svg>
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Submit button */}
        <button
          onClick={handleSubmit}
          disabled={!canSubmit}
          className="btn-primary w-full text-sm mb-6"
        >
          {loading ? (
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

        {/* Results */}
        <AnimatePresence>
          {results && results.length > 0 && (
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              className="space-y-3"
            >
              <h3 className="text-sm font-medium text-slate-300 mb-2">Results</h3>
              {results.map((r, i) => (
                <PredictionCard key={r.school_name} result={r} index={i} />
              ))}
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </div>
  )
}
