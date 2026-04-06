import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import type { AppMode, ChatMessage, Conversation, Experience, Source } from './types'

const MAX_CONVERSATIONS = 50

interface Store {
  // ---- Persisted ----
  conversations: Record<string, Conversation>
  conversationOrder: string[] // sorted by recency (newest first)
  experiences: Experience[]
  activeConversationId: string | null

  // ---- Ephemeral ----
  mode: AppMode
  isConnected: boolean
  collegeOptions: string[]
  streamingContent: string
  streamingLoading: boolean
  sidebarOpen: boolean

  // ---- Actions: mode ----
  setMode: (mode: AppMode) => void

  // ---- Actions: conversations ----
  createConversation: (mode: 'qa' | 'essay') => string
  setActiveConversation: (id: string | null) => void
  deleteConversation: (id: string) => void
  addMessage: (conversationId: string, message: ChatMessage) => void
  updateConversationCollege: (id: string, college: string | null) => void
  updateConversationEssayPrompt: (id: string, prompt: string) => void

  // ---- Actions: streaming ----
  appendStreamingContent: (token: string) => void
  clearStreaming: () => void
  setStreamingLoading: (loading: boolean) => void

  // ---- Actions: experiences ----
  addExperience: (exp: Experience) => void
  updateExperience: (id: string, exp: Partial<Experience>) => void
  deleteExperience: (id: string) => void

  // ---- Actions: UI ----
  setIsConnected: (connected: boolean) => void
  setCollegeOptions: (options: string[]) => void
  setSidebarOpen: (open: boolean) => void
}

export const useStore = create<Store>()(
  persist(
    (set, get) => ({
      // ---- Persisted defaults ----
      conversations: {},
      conversationOrder: [],
      experiences: [],
      activeConversationId: null,

      // ---- Ephemeral defaults ----
      mode: 'qa',
      isConnected: false,
      collegeOptions: [],
      streamingContent: '',
      streamingLoading: false,
      sidebarOpen: true,

      // ---- Mode ----
      setMode: (mode) => set({ mode, activeConversationId: null }),

      // ---- Conversations ----
      createConversation: (mode) => {
        const id = crypto.randomUUID()
        const now = Date.now()
        const conversation: Conversation = {
          id,
          title: 'New Chat',
          mode,
          messages: [],
          college: null,
          essayPrompt: '',
          createdAt: now,
          updatedAt: now,
        }

        set((state) => {
          const conversations = { ...state.conversations, [id]: conversation }
          let order = [id, ...state.conversationOrder]

          // LRU eviction
          if (order.length > MAX_CONVERSATIONS) {
            const removed = order.slice(MAX_CONVERSATIONS)
            for (const rid of removed) {
              delete conversations[rid]
            }
            order = order.slice(0, MAX_CONVERSATIONS)
          }

          return {
            conversations,
            conversationOrder: order,
            activeConversationId: id,
          }
        })

        return id
      },

      setActiveConversation: (id) => set({ activeConversationId: id }),

      deleteConversation: (id) =>
        set((state) => {
          const conversations = { ...state.conversations }
          delete conversations[id]
          const order = state.conversationOrder.filter((cid) => cid !== id)
          return {
            conversations,
            conversationOrder: order,
            activeConversationId:
              state.activeConversationId === id ? null : state.activeConversationId,
          }
        }),

      addMessage: (conversationId, message) =>
        set((state) => {
          const conv = state.conversations[conversationId]
          if (!conv) return state

          const updated = {
            ...conv,
            messages: [...conv.messages, message],
            updatedAt: Date.now(),
            // Auto-title from first user message
            title:
              conv.messages.length === 0 && message.role === 'user'
                ? message.content.slice(0, 60) + (message.content.length > 60 ? '...' : '')
                : conv.title,
          }

          // Bump to front of order
          const order = [
            conversationId,
            ...state.conversationOrder.filter((id) => id !== conversationId),
          ]

          return {
            conversations: { ...state.conversations, [conversationId]: updated },
            conversationOrder: order,
          }
        }),

      updateConversationCollege: (id, college) =>
        set((state) => {
          const conv = state.conversations[id]
          if (!conv) return state
          return {
            conversations: {
              ...state.conversations,
              [id]: { ...conv, college },
            },
          }
        }),

      updateConversationEssayPrompt: (id, prompt) =>
        set((state) => {
          const conv = state.conversations[id]
          if (!conv) return state
          return {
            conversations: {
              ...state.conversations,
              [id]: { ...conv, essayPrompt: prompt },
            },
          }
        }),

      // ---- Streaming ----
      appendStreamingContent: (token) =>
        set((state) => ({ streamingContent: state.streamingContent + token })),
      clearStreaming: () => set({ streamingContent: '', streamingLoading: false }),
      setStreamingLoading: (loading) => set({ streamingLoading: loading }),

      // ---- Experiences ----
      addExperience: (exp) =>
        set((state) => ({ experiences: [...state.experiences, exp] })),

      updateExperience: (id, partial) =>
        set((state) => ({
          experiences: state.experiences.map((e) =>
            e.id === id ? { ...e, ...partial } : e,
          ),
        })),

      deleteExperience: (id) =>
        set((state) => ({
          experiences: state.experiences.filter((e) => e.id !== id),
        })),

      // ---- UI ----
      setIsConnected: (isConnected) => set({ isConnected }),
      setCollegeOptions: (collegeOptions) => set({ collegeOptions }),
      setSidebarOpen: (sidebarOpen) => set({ sidebarOpen }),
    }),
    {
      name: 'college-ai-store',
      partialize: (state) => ({
        conversations: state.conversations,
        conversationOrder: state.conversationOrder,
        experiences: state.experiences,
        activeConversationId: state.activeConversationId,
      }),
    },
  ),
)
