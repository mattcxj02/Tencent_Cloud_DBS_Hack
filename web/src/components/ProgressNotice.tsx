import { useEffect, useState } from 'react'
import { STAGES, type Progress } from '../progress'
import { useAnnounce } from '../session'
import { Notice } from './ui'

export function ProgressNotice({ progress }: { progress: Progress | null }) {
  const announce = useAnnounce()
  const [extra, setExtra] = useState(0)
  useEffect(() => {
    setExtra(0)
    if (!progress || progress.done) return
    const start = Date.now()
    const timer = window.setInterval(() => setExtra(Math.floor((Date.now() - start) / 1000)), 1000)
    return () => window.clearInterval(timer)
  }, [progress])
  const stage = progress?.stage
  useEffect(() => { if (stage) announce(STAGES[stage]) }, [stage, announce])
  return <Notice kind="info" title={stage ? STAGES[stage] : 'Waiting for gateway progress'}>
    {progress && <p role="timer" aria-live="off">Elapsed: {Math.floor(progress.elapsed_seconds + extra)} seconds</p>}
  </Notice>
}
