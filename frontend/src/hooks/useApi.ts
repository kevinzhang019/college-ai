import { useEffect } from 'react'
import { checkHealth, getOptions, getVectorSchools } from '../api'
import { useStore } from '../store'

export function useApi() {
  const setIsConnected = useStore((s) => s.setIsConnected)
  const setCollegeOptions = useStore((s) => s.setCollegeOptions)
  const setVectorSchools = useStore((s) => s.setVectorSchools)

  useEffect(() => {
    let cancelled = false

    async function init() {
      const healthy = await checkHealth()
      if (cancelled) return
      setIsConnected(healthy)

      const [options, vectorSchools] = await Promise.all([
        getOptions(),
        getVectorSchools(),
      ])
      if (cancelled) return
      setCollegeOptions(options.colleges, options.school_states)
      setVectorSchools(vectorSchools)
    }

    init()
    return () => { cancelled = true }
  }, [setIsConnected, setCollegeOptions, setVectorSchools])
}
