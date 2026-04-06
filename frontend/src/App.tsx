import { useApi } from './hooks/useApi'
import { useStore } from './store'
import Sidebar from './components/Sidebar'
import ChatView from './components/ChatView'
import InputArea from './components/InputArea'
import ExperiencesView from './components/ExperiencesView'
import AdmissionsView from './components/AdmissionsView'
import ErrorBoundary from './components/ErrorBoundary'

export default function App() {
  useApi()
  const mode = useStore((s) => s.mode)
  const sidebarOpen = useStore((s) => s.sidebarOpen)
  const setSidebarOpen = useStore((s) => s.setSidebarOpen)

  return (
    <div className="flex h-screen bg-dark-950 overflow-hidden">
      <ErrorBoundary>
        <Sidebar />

        {/* Main content */}
        <main className="flex-1 flex flex-col min-w-0">
          {/* Mobile header with hamburger */}
          <div className="lg:hidden flex items-center gap-3 px-4 py-3 border-b border-dark-700">
            {!sidebarOpen && (
              <button
                onClick={() => setSidebarOpen(true)}
                className="p-1 text-slate-400 hover:text-slate-200"
              >
                <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
                </svg>
              </button>
            )}
            <span className="text-sm font-medium text-slate-300">
              Cole
            </span>
          </div>

          {/* Content area */}
          {mode === 'experiences' ? (
            <ExperiencesView />
          ) : mode === 'admissions' ? (
            <AdmissionsView />
          ) : (
            <>
              <ChatView />
              <InputArea />
            </>
          )}
        </main>
      </ErrorBoundary>
    </div>
  )
}
