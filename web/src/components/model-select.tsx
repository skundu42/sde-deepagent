import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import type { ModelCatalog } from "@/lib/types"

const NONE = "__none__"

/** Grouped model picker over /api/models. Keeps a selected value that is not
 * in the curated catalog (custom yaml entries) selectable. */
export function ModelSelect({
  catalog,
  value,
  onChange,
  emptyLabel,
}: {
  catalog: ModelCatalog
  value: string | null | undefined
  onChange: (v: string | null) => void
  emptyLabel?: string
}) {
  const known = Object.values(catalog).some((info) => info.models.includes(value ?? ""))
  return (
    <Select
      value={value || NONE}
      onValueChange={(v) => onChange(v === NONE ? null : v)}
    >
      <SelectTrigger className="w-full">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {emptyLabel != null && <SelectItem value={NONE}>{emptyLabel}</SelectItem>}
        {Object.entries(catalog).map(([provider, info]) => (
          <SelectGroup key={provider}>
            <SelectLabel>
              {provider}
              {info.configured ? "" : " (no API key)"}
            </SelectLabel>
            {info.models.map((m) => (
              <SelectItem key={m} value={m}>
                {m.split(":")[1] ?? m}
              </SelectItem>
            ))}
          </SelectGroup>
        ))}
        {value && !known ? (
          <SelectGroup>
            <SelectLabel>custom</SelectLabel>
            <SelectItem value={value}>{value}</SelectItem>
          </SelectGroup>
        ) : null}
      </SelectContent>
    </Select>
  )
}
