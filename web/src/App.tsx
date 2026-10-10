import { useState, useEffect, useCallback } from 'react'
import { Chat } from './components/Chat'
import { Confirmation } from './components/Confirmation'
import { Enroll, SignIn } from './components/Auth'
import { Approve } from './components/Approve'

export function App() {
  const [gatewayHealth, setGatewayHealth] = useState<string>('checking…')

  const checkHealth = useCallback(async () => {
    try {
      const res = await fetch('/api/health')
      if (res.ok) {
        const data = await res.json()
        setGatewayHealth(`${data.service}: ${data.status}`)
      } else {
        setGatewayHealth(`HTTP ${res.status}`)
      }
    } catch {
      setGatewayHealth('unreachable')
    }
  }, [])

  useEffect(() => {
    void checkHealth()
  }, [checkHealth])

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-blue-700 text-white px-4 py-3 shadow">
        <h1 className="text-lg font-semibold">DCTA — Direct Conversational Transaction Agent</h1>
        <p className="text-sm text-blue-100">Gateway: {gatewayHealth}</p>
      </header>
      <main className="max-w-2xl mx-auto p-4 space-y-4">
        <Enroll />
        <SignIn />
        <Approve />
        <Chat />
        <Confirmation />
      </main>
    </div>
  )
}
