import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react'
import { Toast } from '../components/ui'

type Tone = 'neutral' | 'ok' | 'danger'

interface ToastItem {
  id: number
  tone: Tone
  message: ReactNode
}

interface ToastContextValue {
  notify: (message: ReactNode, tone?: Tone) => void
}

const ToastContext = createContext<ToastContextValue | null>(null)
let counter = 0

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([])

  const dismiss = useCallback((id: number) => {
    setItems((current) => current.filter((item) => item.id !== id))
  }, [])

  const notify = useCallback(
    (message: ReactNode, tone: Tone = 'neutral') => {
      const id = ++counter
      setItems((current) => [...current, { id, tone, message }])
      window.setTimeout(() => dismiss(id), 6000)
    },
    [dismiss],
  )

  const value = useMemo(() => ({ notify }), [notify])

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="pointer-events-none fixed bottom-5 right-5 z-[60] flex flex-col items-end gap-2">
        {items.map((item) => (
          <Toast
            key={item.id}
            tone={item.tone}
            message={item.message}
            onDismiss={() => dismiss(item.id)}
          />
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast(): ToastContextValue {
  const context = useContext(ToastContext)
  if (!context) throw new Error('useToast must be used inside <ToastProvider>')
  return context
}
