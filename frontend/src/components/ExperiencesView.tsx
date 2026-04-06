import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useStore } from '../store'
import ExperienceForm from './ExperienceForm'
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
  const [showForm, setShowForm] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)

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
