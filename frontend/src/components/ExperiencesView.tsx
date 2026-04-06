import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useStore } from '../store'
import ExperienceForm from './ExperienceForm'
import type { TestScoreType } from '../types'

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
  const [showForm, setShowForm] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [gpaError, setGpaError] = useState('')
  const [scoreError, setScoreError] = useState('')

  const validateGpa = (val: string) => {
    if (!val) { setGpaError(''); return }
    const n = parseFloat(val)
    if (isNaN(n) || n < 0 || n > 5.0) setGpaError('0 – 5.0')
    else setGpaError('')
  }

  const validateScore = (val: string, type: TestScoreType) => {
    if (!val) { setScoreError(''); return }
    const n = parseFloat(val)
    if (type === 'sat' && (isNaN(n) || n < 400 || n > 1600)) setScoreError('400 – 1600')
    else if (type === 'act' && (isNaN(n) || n < 1 || n > 36)) setScoreError('1 – 36')
    else setScoreError('')
  }

  const editingExp = editingId
    ? experiences.find((e) => e.id === editingId) || null
    : null

  return (
    <div className="flex-1 overflow-y-auto custom-scrollbar px-4 py-6">
      <div className="max-w-2xl mx-auto">
        <div className="flex items-center justify-between mb-6">
          <div>
            <h2 className="text-lg font-semibold text-slate-100">
              Your Profile
            </h2>
            <p className="text-sm text-slate-500 mt-0.5">
              These are automatically included as context in Essay mode.
            </p>
          </div>
          <button
            onClick={() => {
              setEditingId(null)
              setShowForm(true)
            }}
            className="btn-primary text-sm px-4 py-2"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
            </svg>
            Add
          </button>
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
                max="5"
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
            <div className="flex-1">
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
        </div>

        {experiences.length === 0 && !showForm && (
          <motion.div
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            className="text-center py-16"
          >
            <span className="text-5xl mb-4 block">📋</span>
            <h3 className="text-lg font-medium text-slate-300 mb-2">
              No experiences yet
            </h3>
            <p className="text-sm text-slate-500 max-w-sm mx-auto">
              Add your extracurriculars, projects, work experience, and
              volunteer activities. They'll help personalize your essay
              brainstorming.
            </p>
          </motion.div>
        )}

        {/* Experience cards */}
        <div className="space-y-3">
          <AnimatePresence>
            {experiences.map((exp) => (
              <motion.div
                key={exp.id}
                layout
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, scale: 0.95 }}
                className="card p-4 group"
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
