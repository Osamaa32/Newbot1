import { useToast } from "@/components/ui/use-toast"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { X } from "lucide-react"

export function Toaster() {
  const { toasts } = useToast()

  if (toasts.length === 0) return null

  return (
    <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2">
      {toasts.map((toast) => (
        <Alert
          key={toast.id}
          variant={toast.variant === "destructive" ? "destructive" : "default"}
          className="w-80 shadow-lg"
        >
          {toast.title && <AlertTitle>{toast.title}</AlertTitle>}
          {toast.description && (
            <AlertDescription>{toast.description}</AlertDescription>
          )}
        </Alert>
      ))}
    </div>
  )
}
