import { useEffect, useState } from 'react'
import { FileText, RotateCw, AlertTriangle, Info, AlertCircle, XCircle } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { useApi } from '@/hooks/useApi'
import { useToast } from '@/components/ui/use-toast'
import { Toaster } from '@/components/ui/toaster'

interface Log {
  id: number
  level: string
  source: string
  message: string
  account_phone?: string
  created_at: string
}

const levelConfig: Record<string, { icon: any; color: string; bg: string }> = {
  info: { icon: Info, color: 'text-blue-500', bg: 'bg-blue-500/10' },
  warning: { icon: AlertTriangle, color: 'text-yellow-500', bg: 'bg-yellow-500/10' },
  error: { icon: AlertCircle, color: 'text-red-500', bg: 'bg-red-500/10' },
  critical: { icon: XCircle, color: 'text-red-700', bg: 'bg-red-700/10' },
}

export default function Logs() {
  const [logs, setLogs] = useState<Log[]>([])
  const [filter, setFilter] = useState('')
  const { get, loading } = useApi()
  const { toast } = useToast()

  const fetchLogs = async () => {
    try {
      const data = await get('/api/logs?limit=100')
      setLogs(data.logs || [])
    } catch {
      toast({ title: 'Error', description: 'Failed to load logs', variant: 'destructive' })
    }
  }

  useEffect(() => {
    fetchLogs()
    const interval = setInterval(fetchLogs, 15000)
    return () => clearInterval(interval)
  }, [])

  const filtered = logs.filter(l =>
    l.message.toLowerCase().includes(filter.toLowerCase()) ||
    l.source.toLowerCase().includes(filter.toLowerCase()) ||
    l.level.toLowerCase().includes(filter.toLowerCase())
  )

  const counts = {
    info: logs.filter(l => l.level === 'info').length,
    warning: logs.filter(l => l.level === 'warning').length,
    error: logs.filter(l => ['error', 'critical'].includes(l.level)).length,
  }

  return (
    <div className="space-y-4">
      <Toaster />
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <FileText className="h-6 w-6" />
            System Logs
          </h1>
          <p className="text-muted-foreground text-sm">Recent activity and events</p>
        </div>
        <Button onClick={fetchLogs} variant="outline" size="icon">
          <RotateCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
        </Button>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-3 gap-4">
        <Card className="bg-blue-500/5 border-blue-500/20">
          <CardContent className="p-3 flex items-center gap-3">
            <Info className="h-5 w-5 text-blue-500" />
            <div>
              <p className="text-lg font-bold">{counts.info}</p>
              <p className="text-xs text-muted-foreground">Info</p>
            </div>
          </CardContent>
        </Card>
        <Card className="bg-yellow-500/5 border-yellow-500/20">
          <CardContent className="p-3 flex items-center gap-3">
            <AlertTriangle className="h-5 w-5 text-yellow-500" />
            <div>
              <p className="text-lg font-bold">{counts.warning}</p>
              <p className="text-xs text-muted-foreground">Warnings</p>
            </div>
          </CardContent>
        </Card>
        <Card className="bg-red-500/5 border-red-500/20">
          <CardContent className="p-3 flex items-center gap-3">
            <XCircle className="h-5 w-5 text-red-500" />
            <div>
              <p className="text-lg font-bold">{counts.error}</p>
              <p className="text-xs text-muted-foreground">Errors</p>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Logs */}
      <Card>
        <CardHeader className="pb-2">
          <input
            type="text"
            placeholder="Filter logs..."
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="px-3 py-2 rounded-md border bg-background text-sm w-full max-w-md"
          />
        </CardHeader>
        <CardContent>
          <div className="space-y-2 max-h-[600px] overflow-y-auto">
            {filtered.length === 0 ? (
              <div className="text-center text-muted-foreground py-8">
                No logs found
              </div>
            ) : (
              filtered.map((log) => {
                const config = levelConfig[log.level] || levelConfig.info
                const Icon = config.icon
                return (
                  <div
                    key={log.id}
                    className={`flex items-start gap-3 p-3 rounded-lg ${config.bg}`}
                  >
                    <Icon className={`h-4 w-4 mt-0.5 ${config.color}`} />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 mb-1">
                        <Badge variant="outline" className={`text-xs ${config.color}`}>
                          {log.level}
                        </Badge>
                        <span className="text-xs text-muted-foreground">
                          {log.source}
                        </span>
                        {log.account_phone && (
                          <span className="text-xs text-muted-foreground font-mono">
                            {log.account_phone}
                          </span>
                        )}
                        <span className="text-xs text-muted-foreground ml-auto">
                          {new Date(log.created_at).toLocaleTimeString()}
                        </span>
                      </div>
                      <p className="text-sm">{log.message}</p>
                    </div>
                  </div>
                )
              })
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
