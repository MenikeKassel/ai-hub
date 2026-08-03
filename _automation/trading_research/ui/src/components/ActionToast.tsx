import { CircleCheck, LoaderCircle, TriangleAlert } from 'lucide-react'

type ActionToastProps = {
  state: 'pending' | 'success' | 'error'
  message: string
}

export default function ActionToast({ state, message }: ActionToastProps) {
  const Icon = state === 'pending' ? LoaderCircle : state === 'success' ? CircleCheck : TriangleAlert
  const cleanMessage = message.replace(/^Error:\s*/i, '')

  return <div className={`action-toast ${state}`} role={state === 'error' ? 'alert' : 'status'} aria-live={state === 'error' ? 'assertive' : 'polite'}>
    <Icon className={state === 'pending' ? 'spin' : undefined} size={18} aria-hidden="true" />
    <span>{cleanMessage}</span>
  </div>
}
