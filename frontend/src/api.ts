import type { AskRequest, AskResponse, Source } from './types'

declare global {
  interface Window {
    COLLEGE_AI_API_URL?: string
  }
}

const API_BASE = window.COLLEGE_AI_API_URL || 'https://api.mommy-soul.com'

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) throw new Error(`API error: ${res.status}`)
  return res.json()
}

export async function checkHealth(): Promise<boolean> {
  try {
    const data = await request<{ status: string }>('/health')
    return data.status === 'ok'
  } catch {
    return false
  }
}

export async function getOptions(): Promise<string[]> {
  try {
    const data = await request<{ colleges: string[] }>('/options')
    return data.colleges
  } catch {
    return FALLBACK_COLLEGES
  }
}

/** Flatten sources that arrive with a nested `entity` sub-object. */
function normalizeSources(raw: Record<string, unknown>[]): Source[] {
  return raw.map((s) => {
    if (s.entity && typeof s.entity === 'object') {
      const { entity, ...rest } = s as Record<string, unknown> & { entity: Record<string, unknown> }
      return { ...rest, ...entity } as unknown as Source
    }
    return s as unknown as Source
  })
}

export async function ask(params: AskRequest): Promise<AskResponse> {
  const res = await request<AskResponse>('/ask', {
    method: 'POST',
    body: JSON.stringify(params),
  })
  if (res.sources) {
    res.sources = normalizeSources(res.sources as unknown as Record<string, unknown>[])
  }
  return res
}

const FALLBACK_COLLEGES = [
  'Stanford University',
  'Harvard University',
  'Massachusetts Institute of Technology',
  'University of California\u2014Berkeley',
  'University of California\u2014Los Angeles',
  'Yale University',
  'Princeton University',
  'Columbia University',
  'University of Chicago',
  'Northwestern University',
  'Cornell University',
  'University of Pennsylvania',
  'Dartmouth College',
  'Brown University',
  'Duke University',
  'Vanderbilt University',
  'Rice University',
  'Carnegie Mellon University',
  'Georgia Institute of Technology',
  'University of Michigan\u2014Ann Arbor',
  'University of Virginia',
  'University of North Carolina\u2014Chapel Hill',
  'University of Texas\u2014Austin',
  'University of Washington',
  'University of Wisconsin\u2014Madison',
  'University of Illinois\u2014Urbana-Champaign',
  'New York University',
  'Boston University',
  'Northeastern University',
  'Rutgers University',
  'University of Southern California',
]
