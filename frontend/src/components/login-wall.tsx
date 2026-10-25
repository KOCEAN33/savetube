import { ArchiveIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { AUTH_ERROR_PARAM, loginUrl } from "@/lib/api"

// Shown instead of the app while there is no session. The login starts only
// on a click, so one tab starts one login.
export function LoginWall() {
  const error = new URLSearchParams(window.location.search).get(AUTH_ERROR_PARAM)

  return (
    <div className="flex min-h-svh items-center justify-center bg-muted p-6">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-lg">
            <ArchiveIcon className="size-5" />
            savetube
          </CardTitle>
          <CardDescription>Log in to continue.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {error && (
            <p className="rounded-lg border border-destructive/50 p-3 text-sm text-destructive">
              Login failed: <span className="font-mono">{error}</span>. Try again.
            </p>
          )}
          <Button asChild className="w-full">
            <a href={loginUrl()}>Log in</a>
          </Button>
        </CardContent>
      </Card>
    </div>
  )
}
