import { Activity, ArrowRight, Database, ListChecks } from 'lucide-react'

type Stage = 'collect' | 'leads' | 'review' | 'audit'

const stages = [
  { key: 'collect', label: '帖子采集', detail: '原始证据', icon: Database, href: '#/system' },
  { key: 'review', label: '待办审核', detail: '标的与推荐一次确认', icon: ListChecks, href: '#/reviews' },
  { key: 'audit', label: '收益审计', detail: '独立跟踪', icon: Activity, href: '#/backtests' },
] as const

export default function ResearchWorkflow({ current }: { current: Stage }) {
  const activeStage = current === 'leads' ? 'review' : current
  return <nav className="research-workflow" aria-label="KOL研究流程">
    {stages.map((stage, index) => {
      const Icon = stage.icon
      return <div className="workflow-stage-wrap" key={stage.key}>
        <a className={stage.key === activeStage ? 'workflow-stage active' : 'workflow-stage'} href={stage.href}>
          <Icon size={17} />
          <span>{stage.label}<small>{stage.detail}</small></span>
        </a>
        {index < stages.length - 1 && <ArrowRight className="workflow-arrow" size={15} />}
      </div>
    })}
  </nav>
}
