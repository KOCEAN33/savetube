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

// What the user did not finish or should do next, keyed by the OAuth error
// the callback redirects with. The code itself stays visible for the logs.
const AUTH_ERROR_TEXT: Record<string, string> = {
  mismatching_state:
    "The login expired or another tab started a new one. Log in again from this tab.",
  invalid_request:
    "This login was already used, for example after going back or reloading. Log in again.",
  access_denied: "The login was cancelled or refused.",
  login_required: "The login session ended. Log in again.",
}
const AUTH_ERROR_FALLBACK = "The login did not complete. Log in again."

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
              {Object.hasOwn(AUTH_ERROR_TEXT, error)
                ? AUTH_ERROR_TEXT[error]
                : AUTH_ERROR_FALLBACK}{" "}
              <span className="font-mono text-xs opacity-70">({error})</span>
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
