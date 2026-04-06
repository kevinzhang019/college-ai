import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useStore } from '../store'
import { predict } from '../api'
import PredictionCard from './PredictionCard'
import type { PredictionResult, Residency, TestScoreType } from '../types'
import { ALLOWED_MAJORS } from '../types'

interface Props {
  college: string
  onClose: () => void
}

export default function QuickPredictModal({ college, onClose }: Props) {
  const profile = useStore((s) => s.profile)
  const setProfileGpa = useStore((s) => s.setProfileGpa)
  const setProfileTestScore = useStore((s) => s.setProfileTestScore)

  const [major, setMajor] = useState<string | null>(null)
  const [residency, setResidency] = useState<Residency | null>(null)
  const [result, setResult] = useState<PredictionResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [gpaError, setGpaError] = useState('')
  const [scoreError, setScoreError] = useState('')

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

  const canSubmit =
    profile.gpa && !gpaError &&
    profile.testScore && !scoreError &&
    !loading

  const handleCalculate = async () => {
    const gpaValid = validateGpa(profile.gpa)
    const scoreValid = validateScore(profile.testScore, profile.testScoreType)
    if (!gpaValid || !scoreValid) return

    setLoading(true)
    setError(null)
    setResult(null)

    try {
      const params: Parameters<typeof predict>[0] = {
        gpa: parseFloat(profile.gpa),
        school_name: college,
        ...(profile.testScoreType === 'sat'
          ? { sat: parseFloat(profile.testScore) }
          : { act: parseFloat(profile.testScore) }),
        ...(residency ? { residency } : {}),
        ...(major ? { major } : {}),
      }

      const data = await predict(params)
      setResult(data)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Something went wrong')
    } finally {
      setLoading(false)
    }
  }

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60"
      onClick={onClose}
    >
      <motion.div
        initial={{ scale: 0.95, opacity: 0 }}
        animate={{ scale: 1, opacity: 1 }}
        exit={{ scale: 0.95, opacity: 0 }}
        transition={{ type: 'spring', damping: 25, stiffness: 300 }}
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-md bg-dark-900 rounded-2xl border border-dark-700 shadow-dark-lg p-6 space-y-4 max-h-[90vh] overflow-y-auto"
      >
        <h3 className="text-lg font-semibold text-slate-100">
          Quick Estimate
        </h3>
        <p className="text-sm text-slate-400 -mt-2">{college}</p>

        {/* GPA + Test score */}
        <div className="flex gap-3 items-start">
          <div className="w-24">
            <label className="block text-xs font-medium text-slate-400 mb-1">GPA *</label>
            <input
              type="number"
              step="0.01"
              min="0"
              max="5"
              value={profile.gpa}
              onChange={(e) => { setProfileGpa(e.target.value); setGpaError('') }}
              onBlur={(e) => validateGpa(e.target.value)}
              placeholder="3.8"
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
                className={`px-2.5 py-2 text-xs font-medium transition-colors ${
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
                className={`px-2.5 py-2 text-xs font-medium transition-colors ${
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
              {profile.testScoreType === 'sat' ? 'SAT *' : 'ACT *'}
            </label>
            <input
              type="number"
              value={profile.testScore}
              onChange={(e) => { setProfileTestScore(profile.testScoreType, e.target.value); setScoreError('') }}
              onBlur={(e) => validateScore(e.target.value, profile.testScoreType)}
              placeholder={profile.testScoreType === 'sat' ? '400–1600' : '1–36'}
              className={`input-field-compact text-sm ${scoreError ? 'border-red-500/60 focus:ring-red-500/40 focus:border-red-500' : ''}`}
            />
            {scoreError && <p className="text-[10px] text-red-400 mt-0.5">{scoreError}</p>}
          </div>
        </div>

        {/* Major + Residency */}
        <div className="flex gap-3">
          <div className="flex-1">
            <label className="block text-xs font-medium text-slate-400 mb-1">Major</label>
            <select
              value={major || ''}
              onChange={(e) => setMajor(e.target.value || null)}
              className="input-field-compact text-sm"
            >
              <option value="">Not specified</option>
              {ALLOWED_MAJORS.map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
          </div>
          <div className="w-36">
            <label className="block text-xs font-medium text-slate-400 mb-1">Residency</label>
            <select
              value={residency || ''}
              onChange={(e) => setResidency((e.target.value || null) as Residency | null)}
              className="input-field-compact text-sm"
            >
              <option value="">Not specified</option>
              <option value="inState">In-State</option>
              <option value="outOfState">Out-of-State</option>
            </select>
          </div>
        </div>

        {/* Calculate button */}
        <button
          onClick={handleCalculate}
          disabled={!canSubmit}
          className="btn-primary w-full text-sm"
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
            'Calculate'
          )}
        </button>

        {/* Error */}
        {error && (
          <p className="text-sm text-red-400">{error}</p>
        )}

        {/* Result */}
        <AnimatePresence>
          {result && <PredictionCard result={result} />}
        </AnimatePresence>

        {/* Close */}
        <div className="flex justify-end pt-1">
          <button onClick={onClose} className="btn-secondary text-sm">
            Close
          </button>
        </div>
      </motion.div>
    </motion.div>
  )
}
