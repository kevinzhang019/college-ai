import type { Config } from 'tailwindcss'

const config: Config = {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        cream: '#fffbeb',
        sand: '#fef3c7',
        coral: {
          50: '#fff1f2',
          100: '#ffe4e6',
          400: '#fb7185',
          500: '#f87171',
        },
        warm: {
          50: '#fafaf9',
          100: '#f5f5f4',
          200: '#e7e5e4',
          300: '#d6d3d1',
          400: '#a8a29e',
          500: '#78716c',
          600: '#57534e',
          700: '#44403c',
          800: '#292524',
          900: '#1c1917',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'sans-serif'],
      },
      boxShadow: {
        'warm-sm': '0 1px 2px 0 rgba(217, 119, 6, 0.05)',
        'warm': '0 1px 3px 0 rgba(217, 119, 6, 0.1), 0 1px 2px -1px rgba(217, 119, 6, 0.1)',
        'warm-md': '0 4px 6px -1px rgba(217, 119, 6, 0.1), 0 2px 4px -2px rgba(217, 119, 6, 0.1)',
        'warm-lg': '0 10px 15px -3px rgba(217, 119, 6, 0.1), 0 4px 6px -4px rgba(217, 119, 6, 0.1)',
      },
      animation: {
        'bounce-dot': 'bounce-dot 1.4s infinite ease-in-out both',
        'fade-in': 'fade-in 0.3s ease-out',
        'slide-up': 'slide-up 0.4s ease-out',
        'pulse-soft': 'pulse-soft 2s infinite',
      },
      keyframes: {
        'bounce-dot': {
          '0%, 80%, 100%': { transform: 'scale(0)' },
          '40%': { transform: 'scale(1)' },
        },
        'fade-in': {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        'slide-up': {
          '0%': { opacity: '0', transform: 'translateY(10px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        'pulse-soft': {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0.6' },
        },
      },
    },
  },
  plugins: [],
}

export default config
