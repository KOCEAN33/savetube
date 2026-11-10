import { useMutation, useQueryClient } from "@tanstack/react-query"

import { mutations } from "@/lib/api"
import { queryKeys } from "@/lib/query-keys"

// Every list of failures reads one dismissal state (docs/plan/frontend.md,
// Home), so a dismissal refetches each of them.
export function useDismiss() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: mutations.dismissAttention,
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.attention() })
      void queryClient.invalidateQueries({ queryKey: queryKeys.jobs() })
      void queryClient.invalidateQueries({ queryKey: queryKeys.encodes() })
    },
  })
}
