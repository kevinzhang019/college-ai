export interface AskRequest {
  question: string
  top_k?: number
  college?: string
  essay_text?: string
}

export interface Source {
  college_name: string
  url: string
  title: string
  content: string
  crawled_at?: string
  distance: number
  url_canonical?: string
  page_type?: string
  rerank_score?: number
}

export interface AskResponse {
  answer: string
  sources: Source[]
  confidence: 'high' | 'medium' | 'low'
  source_count: number
  query_type: 'qa' | 'essay_ideas' | 'essay_review' | 'admission_prediction'
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  sources?: Source[]
  confidence?: 'high' | 'medium' | 'low'
  essayPrompt?: string
  essayText?: string
  hasEssayDraft?: boolean
  timestamp: number
}

// ---- New types for v2 revamp ----

export type AppMode = 'qa' | 'admissions' | 'experiences'

export type ContextSize = 'XS' | 'S' | 'M' | 'L' | 'XL'

export const CONTEXT_SIZE_MAP: Record<ContextSize, number> = {
  XS: 3,
  S: 5,
  M: 8,
  L: 12,
  XL: 16,
}

export type ResponseLength = 'XS' | 'S' | 'M' | 'L' | 'XL'

export const RESPONSE_LENGTH_LABELS: Record<ResponseLength, string> = {
  XS: 'XS',
  S: 'S',
  M: 'M',
  L: 'L',
  XL: 'XL',
}

export type TestScoreType = 'sat' | 'act'

export interface ProfileData {
  gpa: string
  testScoreType: TestScoreType
  testScore: string
  country: string
  countryLabel: string
  state: string
  preferredMajors: string[]
  savedSchools: string[]
}

export type Residency = 'inState' | 'outOfState' | 'international'

export interface SelectedSchool {
  id: string
  name: string
  residency: Residency | null
  major: string | null
}

export interface PredictionFactor {
  factor: string
  impact: 'positive' | 'negative'
  detail: string
}

export type AdmissionClassification = 'safety' | 'match' | 'reach'

export interface PredictionResult {
  probability: number
  confidence_interval: [number, number]
  classification: AdmissionClassification
  school_name: string
  school_acceptance_rate: number
  factors: PredictionFactor[]
  error?: string
}

export const ALLOWED_MAJORS: string[] = [
  'Computer Science', 'Engineering', 'Nursing', 'Data Science',
  'Biology', 'Chemistry', 'Physics', 'Mathematics',
  'Environmental Science', 'Neuroscience', 'Biochemistry', 'Statistics', 'Astronomy',
  'Health Professions', 'Public Health', 'Kinesiology and Physical Therapy', 'Pharmacy',
  'Business and Management', 'Finance and Accounting', 'Economics', 'Marketing',
  'Hospitality', 'Real Estate',
  'English', 'History', 'Philosophy', 'Art', 'Music', 'Theater',
  'Film and Photography', 'Foreign Languages', 'Religious Studies', 'Liberal Arts',
  'Performing Arts', 'Communications', 'Journalism', 'Architecture',
  'Psychology', 'Political Science', 'Sociology', 'Anthropology',
  'Social Work', 'International Relations', 'Gender Studies', 'Public Policy',
  'Education',
  'Criminal Justice', 'Agriculture', 'Culinary Arts', 'Information Technology', 'Aviation',
]

export interface Conversation {
  id: string
  title: string
  mode: 'qa' | 'essay'
  messages: ChatMessage[]
  college: string | null
  essayPrompt: string
  createdAt: number
  updatedAt: number
}

export type ExperienceType = 'extracurricular' | 'project' | 'work' | 'volunteer'

export interface Experience {
  id: string
  title: string
  organization: string
  type: ExperienceType
  description: string
  startDate: string
  endDate: string
}

export interface AskStreamRequest {
  question: string
  top_k?: number
  response_length?: string
  college?: string
  essay_text?: string
  essay_prompt?: string
  history?: { role: string; content: string }[]
  experiences?: Experience[]
}

export type SSEEvent =
  | { type: 'token'; content: string }
  | { type: 'answer_replaced'; content: string }
  | { type: 'sources'; sources: Source[]; confidence: string; query_type: string; reranked?: boolean }
  | { type: 'done' }
  | { type: 'error'; message: string }
