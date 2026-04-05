import { useEffect } from 'react'
import { checkHealth, getOptions } from '../api'
import { useStore } from '../store'

export function useApi() {
  const setIsConnected = useStore((s) => s.setIsConnected)
  const setCollegeOptions = useStore((s) => s.setCollegeOptions)

  useEffect(() => {
    let cancelled = false

    async function init() {
      const healthy = await checkHealth()
      if (cancelled) return
      setIsConnected(healthy)

      const options = await getOptions()
      if (cancelled) return
      setCollegeOptions(options)
    }

    init()
    return () => { cancelled = true }
  }, [setIsConnected, setCollegeOptions])
}
