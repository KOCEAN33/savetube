import { useState, type FormEvent } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { PencilIcon, PlusIcon, Trash2Icon } from "lucide-react"

import { ErrorBox, Page, PageSection, QueryState } from "@/components/query-state"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { api, mutations, type Rule, type RuleIn, type VideoType } from "@/lib/api"

const TYPES: VideoType[] = ["videos", "shorts", "lives", "members"]

const EMPTY: RuleIn = {
  name: "",
  types: ["videos", "shorts", "lives"],
  date_after: null,
  format: "bv*+ba/b",
  use_cookies: false,
  watch_lives: false,
  max_concurrent: 1,
}

function yesNo(value: boolean): string {
  return value ? "yes" : "no"
}

function RuleForm({ rule, onDone }: { rule: Rule | null; onDone: () => void }) {
  const queryClient = useQueryClient()
  const [form, setForm] = useState<RuleIn>(rule ?? EMPTY)
  const save = useMutation({
    mutationFn: (body: RuleIn) =>
      rule ? mutations.replaceRule(rule.id, body) : mutations.createRule(body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["rules"] })
      onDone()
    },
  })

  function toggleType(type: VideoType, on: boolean) {
    setForm((f) => ({
      ...f,
      types: on ? TYPES.filter((t) => t === type || f.types.includes(t)) : f.types.filter((t) => t !== type),
    }))
  }

  function submit(e: FormEvent) {
    e.preventDefault()
    save.mutate(form)
  }

  return (
    <form onSubmit={submit} className="flex flex-1 flex-col gap-4 overflow-y-auto px-4">
      <div className="flex flex-col gap-2">
        <Label htmlFor="rule-name">Name</Label>
        <Input
          id="rule-name"
          required
          value={form.name}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
        />
      </div>
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-2 text-sm font-medium">Types to keep</legend>
        {TYPES.map((t) => (
          <Label key={t} className="font-normal">
            <Checkbox
              checked={form.types.includes(t)}
              onCheckedChange={(on) => toggleType(t, on === true)}
            />
            {t}
          </Label>
        ))}
      </fieldset>
      <div className="flex flex-col gap-2">
        <Label htmlFor="rule-date">Uploaded on or after</Label>
        <Input
          id="rule-date"
          type="date"
          value={form.date_after ?? ""}
          onChange={(e) => setForm({ ...form, date_after: e.target.value || null })}
        />
      </div>
      <div className="flex flex-col gap-2">
        <Label htmlFor="rule-format">Format</Label>
        <Input
          id="rule-format"
          required
          className="font-mono"
          value={form.format}
          onChange={(e) => setForm({ ...form, format: e.target.value })}
        />
      </div>
      <Label className="font-normal">
        <Checkbox
          checked={form.use_cookies}
          onCheckedChange={(on) => setForm({ ...form, use_cookies: on === true })}
        />
        Use account cookies (members-only content)
      </Label>
      <Label className="font-normal">
        <Checkbox
          checked={form.watch_lives}
          onCheckedChange={(on) => setForm({ ...form, watch_lives: on === true })}
        />
        Watch for lives
      </Label>
      {save.isError && <ErrorBox error={save.error} />}
      <SheetFooter className="px-0">
        <Button type="submit" disabled={save.isPending || form.types.length === 0}>
          {rule ? "Save" : "Create rule"}
        </Button>
      </SheetFooter>
    </form>
  )
}

export function RulesPage() {
  const queryClient = useQueryClient()
  const rules = useQuery({ queryKey: ["rules"], queryFn: api.rules })
  // `null` is a new rule; `undefined` is a closed sheet.
  const [editing, setEditing] = useState<Rule | null | undefined>(undefined)
  const remove = useMutation({
    mutationFn: mutations.deleteRule,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["rules"] }),
  })

  const actions = (
    <Button size="sm" onClick={() => setEditing(null)}>
      <PlusIcon /> New rule
    </Button>
  )

  return (
    <Page>
      {remove.isError && <ErrorBox error={remove.error} />}
      <PageSection title="Rules" actions={actions}>
        <QueryState query={rules}>
          {(rows) => (
            <Table>
              <TableHeader className="bg-muted">
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Types</TableHead>
                  <TableHead>Uploaded after</TableHead>
                  <TableHead>Format</TableHead>
                  <TableHead>Cookies</TableHead>
                  <TableHead>Watch lives</TableHead>
                  <TableHead className="text-right">Max concurrent</TableHead>
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={8} className="text-center text-muted-foreground">
                      No rules yet. A source needs one.
                    </TableCell>
                  </TableRow>
                )}
                {rows.map((r) => (
                  <TableRow key={r.id}>
                    <TableCell className="font-medium">{r.name}</TableCell>
                    <TableCell className="flex flex-wrap gap-1">
                      {r.types.map((t) => (
                        <Badge key={t} variant="outline">
                          {t}
                        </Badge>
                      ))}
                    </TableCell>
                    <TableCell>{r.date_after ?? "-"}</TableCell>
                    <TableCell className="font-mono text-xs">{r.format}</TableCell>
                    <TableCell>{yesNo(r.use_cookies)}</TableCell>
                    <TableCell>{yesNo(r.watch_lives)}</TableCell>
                    <TableCell className="text-right">{r.max_concurrent}</TableCell>
                    <TableCell className="text-right whitespace-nowrap">
                      <Button
                        variant="ghost"
                        size="icon-sm"
                        aria-label={`Edit ${r.name}`}
                        onClick={() => setEditing(r)}
                      >
                        <PencilIcon />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon-sm"
                        aria-label={`Delete ${r.name}`}
                        disabled={remove.isPending}
                        onClick={() => {
                          if (window.confirm(`Delete rule "${r.name}"?`)) remove.mutate(r.id)
                        }}
                      >
                        <Trash2Icon />
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </QueryState>
      </PageSection>
      <Sheet open={editing !== undefined} onOpenChange={(open) => !open && setEditing(undefined)}>
        <SheetContent>
          <SheetHeader>
            <SheetTitle>{editing ? `Edit ${editing.name}` : "New rule"}</SheetTitle>
            <SheetDescription>
              A source archives the types its rule keeps. Live watching is not built yet.
            </SheetDescription>
          </SheetHeader>
          {editing !== undefined && (
            <RuleForm
              key={editing?.id ?? "new"}
              rule={editing}
              onDone={() => setEditing(undefined)}
            />
          )}
        </SheetContent>
      </Sheet>
    </Page>
  )
}
