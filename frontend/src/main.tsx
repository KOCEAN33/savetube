import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import {
  MutationCache,
  QueryCache,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query"
import { RouterProvider } from "react-router"

import { TooltipProvider } from "@/components/ui/tooltip"
import { ApiError } from "@/lib/api"
import { queryKeys } from "@/lib/query-keys"
import { router } from "@/router"
import "./index.css"

// A session that ends while the app is open: any 401 refetches /api/me, and
// its 401 replaces the app with the login wall.
function onError(error: Error) {
  if (error instanceof ApiError && error.status === 401) {
    void queryClient.invalidateQueries({ queryKey: queryKeys.me(), refetchType: "all" })
  }
}

const queryClient: QueryClient = new QueryClient({
  queryCache: new QueryCache({
    onError: (error, query) => {
      if (query.queryKey[0] !== queryKeys.me()[0]) onError(error)
    },
  }),
  mutationCache: new MutationCache({ onError }),
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      // A 4xx will not change on retry. A 401 shows the login wall.
      retry: (failureCount, error) =>
        !(error instanceof ApiError && error.status < 500) && failureCount < 2,
    },
  },
})

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <RouterProvider router={router} />
      </TooltipProvider>
    </QueryClientProvider>
  </StrictMode>
)
