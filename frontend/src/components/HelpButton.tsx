import { motion } from 'framer-motion'
import { useStore } from '../store'

export default function HelpButton() {
  const setHelpOpen = useStore((s) => s.setHelpOpen)

  return (
    <motion.button
      whileHover={{ scale: 1.1 }}
      whileTap={{ scale: 0.9 }}
      onClick={() => setHelpOpen(true)}
      className="fixed bottom-6 right-6 w-12 h-12 bg-amber-500 hover:bg-amber-600 text-white rounded-full shadow-warm-lg flex items-center justify-center text-lg font-bold transition-colors z-30"
      title="Example questions"
    >
      ?
    </motion.button>
  )
}
