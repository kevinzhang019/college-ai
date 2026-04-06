import { useState } from 'react'
import { motion } from 'framer-motion'
import { useStore } from '../store'
import type { Experience, ExperienceType } from '../types'

const TYPES: { value: ExperienceType; label: string }[] = [
  { value: 'extracurricular', label: 'Extracurricular' },
  { value: 'project', label: 'Project' },
  { value: 'work', label: 'Work Experience' },
  { value: 'volunteer', label: 'Volunteer' },
]

interface Props {
  experience: Experience | null
  onClose: () => void
}

export default function ExperienceForm({ experience, onClose }: Props) {
  const addExperience = useStore((s) => s.addExperience)
  const updateExperience = useStore((s) => s.updateExperience)

  const [title, setTitle] = useState(experience?.title || '')
  const [organization, setOrganization] = useState(experience?.organization || '')
  const [type, setType] = useState<ExperienceType>(experience?.type || 'extracurricular')
  const [description, setDescription] = useState(experience?.description || '')
  const [startDate, setStartDate] = useState(experience?.startDate || '')
  const [endDate, setEndDate] = useState(experience?.endDate || '')
  const [isPresent, setIsPresent] = useState(experience?.endDate === 'Present')

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!title.trim()) return

    const data: Experience = {
      id: experience?.id || crypto.randomUUID(),
      title: title.trim(),
      organization: organization.trim(),
      type,
      description: description.trim(),
      startDate,
      endDate: isPresent ? 'Present' : endDate,
    }

    if (experience) {
      updateExperience(experience.id, data)
    } else {
      addExperience(data)
    }

    onClose()
  }

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60"
      onClick={onClose}
    >
      <motion.form
        initial={{ scale: 0.95, opacity: 0 }}
        animate={{ scale: 1, opacity: 1 }}
        exit={{ scale: 0.95, opacity: 0 }}
        transition={{ type: 'spring', damping: 25, stiffness: 300 }}
        onClick={(e) => e.stopPropagation()}
        onSubmit={handleSubmit}
        className="w-full max-w-lg bg-dark-900 rounded-2xl border border-dark-700 shadow-dark-lg p-6 space-y-4"
      >
        <h3 className="text-lg font-semibold text-slate-100">
          {experience ? 'Edit Experience' : 'Add Experience'}
        </h3>

        {/* Title */}
        <div>
          <label className="block text-xs font-medium text-slate-400 mb-1">
            Title *
          </label>
          <input
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="e.g., Math Olympiad Team Captain"
            className="input-field text-sm"
            autoFocus
          />
        </div>

        {/* Organization + Type */}
        <div className="flex gap-3">
          <div className="flex-1">
            <label className="block text-xs font-medium text-slate-400 mb-1">
              Organization
            </label>
            <input
              type="text"
              value={organization}
              onChange={(e) => setOrganization(e.target.value)}
              placeholder="e.g., School Club, Company"
              className="input-field text-sm"
            />
          </div>
          <div className="w-40">
            <label className="block text-xs font-medium text-slate-400 mb-1">
              Type
            </label>
            <select
              value={type}
              onChange={(e) => setType(e.target.value as ExperienceType)}
              className="input-field text-sm"
            >
              {TYPES.map((t) => (
                <option key={t.value} value={t.value}>
                  {t.label}
                </option>
              ))}
            </select>
          </div>
        </div>

        {/* Dates */}
        <div className="flex gap-3 items-end">
          <div className="flex-1">
            <label className="block text-xs font-medium text-slate-400 mb-1">
              Start Date
            </label>
            <input
              type="month"
              value={startDate}
              onChange={(e) => setStartDate(e.target.value)}
              className="input-field text-sm"
            />
          </div>
          <div className="flex-1">
            <label className="block text-xs font-medium text-slate-400 mb-1">
              End Date
            </label>
            <input
              type="month"
              value={isPresent ? '' : endDate}
              onChange={(e) => setEndDate(e.target.value)}
              disabled={isPresent}
              className="input-field text-sm disabled:opacity-50"
            />
          </div>
          <label className="flex items-center gap-1.5 pb-3 cursor-pointer shrink-0">
            <input
              type="checkbox"
              checked={isPresent}
              onChange={(e) => setIsPresent(e.target.checked)}
              className="rounded border-dark-700 bg-dark-800 text-forest-500 focus:ring-forest-500/40"
            />
            <span className="text-xs text-slate-400">Present</span>
          </label>
        </div>

        {/* Description */}
        <div>
          <label className="block text-xs font-medium text-slate-400 mb-1">
            Description
          </label>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Describe your role, accomplishments, and impact..."
            className="input-field text-sm min-h-[80px] resize-none"
            rows={3}
          />
        </div>

        {/* Actions */}
        <div className="flex gap-2 justify-end pt-2">
          <button type="button" onClick={onClose} className="btn-secondary text-sm">
            Cancel
          </button>
          <button
            type="submit"
            disabled={!title.trim()}
            className="btn-primary text-sm"
          >
            {experience ? 'Save' : 'Add'}
          </button>
        </div>
      </motion.form>
    </motion.div>
  )
}
