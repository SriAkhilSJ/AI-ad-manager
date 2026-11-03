import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiError } from '../lib/api'
import { useAuth } from '../hooks/useAuth'
import { Banner, Button, Input } from '../components/ui'

export function LoginPage() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const [email, setEmail] = useState('founder@crusor.ai')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setPending(true)
    setError(null)
    try {
      await login(email.trim(), password)
      navigate('/', { replace: true })
    } catch (cause) {
      setError(
        cause instanceof ApiError
          ? cause.message
          : 'Sign in failed. Check that the API is reachable.',
      )
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-6 py-12">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl border border-white/[0.12] bg-gradient-to-br from-white/[0.12] to-transparent">
            <span className="text-base font-semibold text-steel-300">C</span>
          </div>
          <div>
            <p className="text-sm font-semibold tracking-tight text-steel-300">CRUSOR</p>
            <p className="text-[10px] uppercase tracking-brand text-steel-600">Marketing HQ</p>
          </div>
        </div>

        <h1 className="text-lg font-semibold tracking-tight text-steel-300">Sign in</h1>
        <p className="mt-1 text-xs leading-relaxed text-steel-500">
          Human approval is required for every piece of content. Sign in with your founder or
          marketer account.
        </p>

        <form onSubmit={submit} className="mt-6 space-y-3">
          <Input
            label="Email"
            type="email"
            autoComplete="username"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            required
          />
          <Input
            label="Password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required
            placeholder="••••••••"
          />

          {error ? <Banner tone="danger">{error}</Banner> : null}

          <Button type="submit" variant="primary" loading={pending} className="w-full">
            Enter Marketing HQ
          </Button>
        </form>

        <div className="mt-6 rounded-lg border border-white/[0.07] bg-white/[0.02] px-3.5 py-3">
          <p className="text-[11px] font-medium text-steel-400">First run?</p>
          <p className="mt-1 text-[11px] leading-relaxed text-steel-600">
            The API bootstraps a founder account on an empty database using
            <span className="hq-mono mx-1">BOOTSTRAP_FOUNDER_EMAIL</span> /
            <span className="hq-mono mx-1">BOOTSTRAP_FOUNDER_PASSWORD</span>. Change that password
            before exposing this instance.
          </p>
        </div>
      </div>
    </div>
  )
}
