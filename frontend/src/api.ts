import type { AskRequest, AskResponse, AskStreamRequest, Source, SSEEvent } from './types'

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

// ---- Streaming SSE client ----

export interface StreamCallbacks {
  onToken: (text: string) => void
  onSources: (sources: Source[], confidence: string, queryType: string) => void
  onDone: () => void
  onError: (message: string) => void
}

export async function askStream(
  params: AskStreamRequest,
  callbacks: StreamCallbacks,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_BASE}/ask/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
    signal,
  })

  if (!res.ok) {
    callbacks.onError(`API error: ${res.status}`)
    return
  }

  const reader = res.body?.getReader()
  if (!reader) {
    callbacks.onError('No response body')
    return
  }

  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })

    // Parse SSE lines
    const lines = buffer.split('\n')
    buffer = lines.pop() || '' // keep incomplete line in buffer

    for (const line of lines) {
      const trimmed = line.trim()
      if (!trimmed.startsWith('data: ')) continue

      try {
        const event: SSEEvent = JSON.parse(trimmed.slice(6))

        switch (event.type) {
          case 'token':
            callbacks.onToken(event.content)
            break
          case 'sources': {
            const sources = normalizeSources(
              event.sources as unknown as Record<string, unknown>[],
            )
            callbacks.onSources(sources, event.confidence, event.query_type)
            break
          }
          case 'done':
            callbacks.onDone()
            break
          case 'error':
            callbacks.onError(event.message)
            break
        }
      } catch {
        // Skip malformed lines
      }
    }
  }
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
