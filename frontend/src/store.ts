import { create } from 'zustand'
import type { AskResponse, ChatMessage } from './types'

interface Store {
  // Global
  mode: 'qa' | 'essay'
  setMode: (mode: 'qa' | 'essay') => void
  essayTab: 'chat' | 'editor'
  setEssayTab: (tab: 'chat' | 'editor') => void
  college: string | null
  setCollege: (college: string | null) => void
  topK: number
  setTopK: (k: number) => void
  isConnected: boolean
  setIsConnected: (connected: boolean) => void
  collegeOptions: string[]
  setCollegeOptions: (options: string[]) => void

  // QA Mode
  qaQuestion: string
  setQaQuestion: (q: string) => void
  qaResult: AskResponse | null
  setQaResult: (r: AskResponse | null) => void
  qaLoading: boolean
  setQaLoading: (l: boolean) => void

  // Essay Chat
  chatMessages: ChatMessage[]
  addChatMessage: (msg: ChatMessage) => void
  chatLoading: boolean
  setChatLoading: (l: boolean) => void

  // Essay Editor
  essayText: string
  setEssayText: (t: string) => void
  editorFeedback: AskResponse | null
  setEditorFeedback: (r: AskResponse | null) => void
  editorLoading: boolean
  setEditorLoading: (l: boolean) => void

  // Help modal
  helpOpen: boolean
  setHelpOpen: (open: boolean) => void
}

export const useStore = create<Store>((set) => ({
  mode: 'qa',
  setMode: (mode) => set({ mode }),
  essayTab: 'chat',
  setEssayTab: (essayTab) => set({ essayTab }),
  college: null,
  setCollege: (college) => set({ college }),
  topK: 8,
  setTopK: (topK) => set({ topK }),
  isConnected: false,
  setIsConnected: (isConnected) => set({ isConnected }),
  collegeOptions: [],
  setCollegeOptions: (collegeOptions) => set({ collegeOptions }),

  qaQuestion: '',
  setQaQuestion: (qaQuestion) => set({ qaQuestion }),
  qaResult: null,
  setQaResult: (qaResult) => set({ qaResult }),
  qaLoading: false,
  setQaLoading: (qaLoading) => set({ qaLoading }),

  chatMessages: [],
  addChatMessage: (msg) =>
    set((state) => ({ chatMessages: [...state.chatMessages, msg] })),
  chatLoading: false,
  setChatLoading: (chatLoading) => set({ chatLoading }),

  essayText: '',
  setEssayText: (essayText) => set({ essayText }),
  editorFeedback: null,
  setEditorFeedback: (editorFeedback) => set({ editorFeedback }),
  editorLoading: false,
  setEditorLoading: (editorLoading) => set({ editorLoading }),

  helpOpen: false,
  setHelpOpen: (helpOpen) => set({ helpOpen }),
}))
