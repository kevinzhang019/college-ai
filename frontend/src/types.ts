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
  timestamp: number
}

// ---- New types for v2 revamp ----

export type AppMode = 'qa' | 'essay' | 'experiences'

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
  college?: string
  essay_text?: string
  essay_prompt?: string
  history?: { role: string; content: string }[]
  experiences?: Experience[]
}

export type SSEEvent =
  | { type: 'token'; content: string }
  | { type: 'sources'; sources: Source[]; confidence: string; query_type: string }
  | { type: 'done' }
  | { type: 'error'; message: string }
