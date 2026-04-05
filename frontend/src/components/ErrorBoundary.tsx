import { Component, type ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  hasError: boolean
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false }

  static getDerivedStateFromError(): State {
    return { hasError: true }
  }

  componentDidCatch(error: Error) {
    console.error('ErrorBoundary caught:', error)
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex flex-col items-center justify-center py-20 px-4">
          <span className="text-4xl mb-4">Something went wrong.</span>
          <p className="text-sm text-warm-400 mb-6">
            An unexpected error occurred while rendering.
          </p>
          <button
            onClick={() => this.setState({ hasError: false })}
            className="btn-primary"
          >
            Try again
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
