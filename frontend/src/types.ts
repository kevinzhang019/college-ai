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
