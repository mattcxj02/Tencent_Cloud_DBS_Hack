import { useState, useRef, useEffect } from 'react'

interface Message {
  id: number
  role: 'user' | 'assistant'
  text: string
}

/**
 * Chat placeholder component.
 * T-09 implements the full conversation workflow;
 * T-12 adds voice/microphone capture.
 */
export function Chat() {
  const [messages, setMessages] = useState<Message[]>([
    { id: 0, role: 'assistant', text: 'Welcome. How can I help you with your banking today?' },
  ])
  const [input, setInput] = useState('')
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages])

  const send = () => {
    if (!input.trim()) return
    const userMsg: Message = { id: messages.length, role: 'user', text: input.trim() }
    setMessages((prev) => [...prev, userMsg])
    setInput('')
    // Placeholder: the real extraction workflow (T-09) replaces this.
    const placeholder: Message = {
      id: messages.length + 1,
      role: 'assistant',
      text: '(Extraction workflow not yet implemented — T-09)',
    }
    setMessages((prev) => [...prev, placeholder])
  }

  return (
    <section className="bg-white rounded-lg shadow p-4">
      <h2 className="text-sm font-medium text-gray-600 mb-2">Conversation</h2>
      <div
        ref={scrollRef}
        className="h-64 overflow-y-auto space-y-2 border rounded p-3 bg-gray-50"
      >
        {messages.map((m) => (
          <div
            key={m.id}
            className={m.role === 'user' ? 'text-right' : 'text-left'}
          >
            <span
              className={
                m.role === 'user'
                  ? 'inline-block bg-blue-600 text-white rounded-lg px-3 py-1.5 text-sm max-w-xs'
                  : 'inline-block bg-gray-200 text-gray-800 rounded-lg px-3 py-1.5 text-sm max-w-xs'
              }
            >
              {m.text}
            </span>
          </div>
        ))}
      </div>
      <div className="flex gap-2 mt-3">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && send()}
          placeholder="Type your request…"
          className="flex-1 border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
        <button
          onClick={send}
          className="bg-blue-600 text-white rounded-lg px-4 py-2 text-sm font-medium hover:bg-blue-700"
        >
          Send
        </button>
      </div>
      {/* T-12: microphone push-to-talk button goes here */}
    </section>
  )
}
