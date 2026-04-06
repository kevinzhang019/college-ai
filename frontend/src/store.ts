import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import type { AppMode, ChatMessage, ContextSize, Conversation, Experience, ProfileData, ResponseLength, TestScoreType } from './types'

const MAX_CONVERSATIONS = 50

interface Store {
  // ---- Persisted ----
  conversations: Record<string, Conversation>
  conversationOrder: string[] // sorted by recency (newest first)
  experiences: Experience[]
  profile: ProfileData
  activeConversationId: string | null
  contextSize: ContextSize
  responseLength: ResponseLength

  // ---- Ephemeral ----
  mode: AppMode
  isConnected: boolean
  collegeOptions: string[]
  schoolStates: Record<string, string>
  streamingContent: string
  streamingLoading: boolean
  sidebarOpen: boolean

  // ---- Actions: mode ----
  setMode: (mode: AppMode) => void
  setContextSize: (size: ContextSize) => void
  setResponseLength: (size: ResponseLength) => void

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

  // ---- Actions: profile ----
  setProfileGpa: (gpa: string) => void
  setProfileTestScore: (type: TestScoreType, score: string) => void
  setProfileLocation: (country: string, countryLabel: string, state: string) => void
  addPreferredMajor: (major: string) => void
  removePreferredMajor: (major: string) => void
  reorderPreferredMajors: (majors: string[]) => void

  // ---- Actions: experiences ----
  addExperience: (exp: Experience) => void
  updateExperience: (id: string, exp: Partial<Experience>) => void
  deleteExperience: (id: string) => void

  // ---- Actions: UI ----
  setIsConnected: (connected: boolean) => void
  setCollegeOptions: (options: string[], schoolStates?: Record<string, string>) => void
  setSidebarOpen: (open: boolean) => void
}

export const useStore = create<Store>()(
  persist(
    (set) => ({
      // ---- Persisted defaults ----
      conversations: {},
      conversationOrder: [],
      experiences: [],
      profile: { gpa: '', testScoreType: 'sat', testScore: '', country: '', countryLabel: '', state: '', preferredMajors: [] },
      activeConversationId: null,
      contextSize: 'M',
      responseLength: 'M',

      // ---- Ephemeral defaults ----
      mode: 'qa',
      isConnected: false,
      collegeOptions: [],
      schoolStates: {},
      streamingContent: '',
      streamingLoading: false,
      sidebarOpen: true,

      // ---- Mode ----
      setMode: (mode) => set({ mode, activeConversationId: null }),
      setContextSize: (contextSize) => set({ contextSize }),
      setResponseLength: (responseLength) => set({ responseLength }),

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

      // ---- Profile ----
      setProfileGpa: (gpa) =>
        set((state) => ({ profile: { ...state.profile, gpa } })),

      setProfileTestScore: (type, score) =>
        set((state) => ({ profile: { ...state.profile, testScoreType: type, testScore: score } })),

      setProfileLocation: (country, countryLabel, state) =>
        set((s) => ({ profile: { ...s.profile, country, countryLabel, state } })),

      addPreferredMajor: (major) =>
        set((s) => {
          if (s.profile.preferredMajors.includes(major)) return s
          return { profile: { ...s.profile, preferredMajors: [...s.profile.preferredMajors, major] } }
        }),

      removePreferredMajor: (major) =>
        set((s) => ({
          profile: { ...s.profile, preferredMajors: s.profile.preferredMajors.filter((m) => m !== major) },
        })),

      reorderPreferredMajors: (majors) =>
        set((s) => ({ profile: { ...s.profile, preferredMajors: majors } })),

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
      setCollegeOptions: (collegeOptions, schoolStates) => set({ collegeOptions, ...(schoolStates ? { schoolStates } : {}) }),
      setSidebarOpen: (sidebarOpen) => set({ sidebarOpen }),
    }),
    {
      name: 'college-ai-store',
      partialize: (state) => ({
        conversations: state.conversations,
        conversationOrder: state.conversationOrder,
        experiences: state.experiences,
        profile: state.profile,
        activeConversationId: state.activeConversationId,
        contextSize: state.contextSize,
        responseLength: state.responseLength,
      }),
    },
  ),
)
