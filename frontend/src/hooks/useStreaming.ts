import { useCallback, useRef } from 'react'
import { useStore } from '../store'
import { askStream } from '../api'
import type { ChatMessage, Source } from '../types'
import { CONTEXT_SIZE_MAP } from '../types'

export function useStreaming() {
  const abortRef = useRef<AbortController | null>(null)

  const send = useCallback(async (question: string, essayText?: string) => {
    const state = useStore.getState()
    const {
      activeConversationId,
      experiences,
      mode,
      addMessage,
      appendStreamingContent,
      clearStreaming,
      setStreamingLoading,
      createConversation,
    } = state

    // Create conversation if none active
    const chatMode = mode === 'essay' ? 'essay' : 'qa'
    const convId = activeConversationId || createConversation(chatMode)
    const conv = useStore.getState().conversations[convId]
    if (!conv) return

    // Add user message
    const userMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: 'user',
      content: question,
      timestamp: Date.now(),
    }
    addMessage(convId, userMsg)

    // Build history from conversation (last 6 messages)
    const recentMessages = conv.messages.slice(-6).map((m) => ({
      role: m.role,
      content: m.content,
    }))

    // Build request
    const request = {
      question,
      top_k: CONTEXT_SIZE_MAP[state.contextSize],
      response_length: state.responseLength,
      ...(conv.college ? { college: conv.college } : {}),
      ...(chatMode === 'essay' ? { essay_prompt: conv.essayPrompt || '' } : {}),
      ...(chatMode === 'essay' && essayText ? { essay_text: essayText } : {}),
      ...(recentMessages.length > 0 ? { history: recentMessages } : {}),
      ...(chatMode === 'essay' && experiences.length > 0 ? { experiences } : {}),
    }

    // Start streaming
    setStreamingLoading(true)
    clearStreaming()

    const controller = new AbortController()
    abortRef.current = controller

    let collectedSources: Source[] = []
    let collectedConfidence: ChatMessage['confidence'] = undefined

    try {
      await askStream(
        request,
        {
          onToken: (text) => {
            appendStreamingContent(text)
          },
          onSources: (sources, confidence) => {
            collectedSources = sources
            collectedConfidence = confidence as ChatMessage['confidence']
          },
          onDone: () => {
            const finalContent = useStore.getState().streamingContent
            const assistantMsg: ChatMessage = {
              id: crypto.randomUUID(),
              role: 'assistant',
              content: finalContent,
              sources: collectedSources,
              confidence: collectedConfidence,
              timestamp: Date.now(),
            }
            addMessage(convId, assistantMsg)
            clearStreaming()
          },
          onError: (message) => {
            const assistantMsg: ChatMessage = {
              id: crypto.randomUUID(),
              role: 'assistant',
              content: `Sorry, something went wrong: ${message}`,
              timestamp: Date.now(),
            }
            addMessage(convId, assistantMsg)
            clearStreaming()
          },
        },
        controller.signal,
      )
    } catch (err) {
      if ((err as Error).name !== 'AbortError') {
        const assistantMsg: ChatMessage = {
          id: crypto.randomUUID(),
          role: 'assistant',
          content: "Sorry, I couldn't process that. Please try again.",
          timestamp: Date.now(),
        }
        addMessage(convId, assistantMsg)
      }
      clearStreaming()
    }

    abortRef.current = null
  }, [])

  const cancel = useCallback(() => {
    abortRef.current?.abort()
    abortRef.current = null
    useStore.getState().clearStreaming()
  }, [])

  return { send, cancel }
}
