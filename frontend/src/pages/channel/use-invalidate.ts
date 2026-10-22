import { useQueryClient } from "@tanstack/react-query"

export function useInvalidate() {
  const queryClient = useQueryClient()
  return () => {
    queryClient.invalidateQueries({ queryKey: ["sources"] })
    queryClient.invalidateQueries({ queryKey: ["channels"] })
  }
}
