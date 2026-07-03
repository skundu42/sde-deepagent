import type { LucideIcon } from "lucide-react"

export function Empty({
  icon: Icon,
  title,
  children,
}: {
  icon: LucideIcon
  title: string
  children?: React.ReactNode
}) {
  return (
    <div className="flex flex-col items-center gap-2 rounded-lg border border-dashed px-6 py-14 text-center">
      <Icon className="size-8 text-muted-foreground/60" strokeWidth={1.5} />
      <div className="font-medium">{title}</div>
      {children ? <div className="max-w-md text-sm text-muted-foreground">{children}</div> : null}
    </div>
  )
}
