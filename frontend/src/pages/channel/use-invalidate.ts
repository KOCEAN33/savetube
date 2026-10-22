import { useQueryClient } from "@tanstack/react-query"
import { queryKeys } from "@/lib/query-keys"

export function useInvalidate() {
  const queryClient = useQueryClient()
  return () => {
    queryClient.invalidateQueries({ queryKey: queryKeys.sources() })
    queryClient.invalidateQueries({ queryKey: queryKeys.channels() })
  }
}
