import { useEffect } from 'react'
import {
  Smartphone, Link2, KeyRound, Users,
  Activity, MessageSquare, Send, AlertTriangle
} from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { useWebSocket } from '@/hooks/useWebSocket'
import { useApi } from '@/hooks/useApi'

export default function Dashboard() {
  const { stats, isConnected, refreshStats } = useWebSocket()
  const { get } = useApi()

  useEffect(() => {
    refreshStats()
    const interval = setInterval(refreshStats, 10000)
    return () => clearInterval(interval)
  }, [refreshStats])

  const statCards = [
    {
      title: 'Total Accounts',
      value: stats?.accounts?.total || 0,
      active: stats?.accounts?.active || 0,
      icon: Smartphone,
      color: 'text-blue-500',
      bg: 'bg-blue-500/10',
    },
    {
      title: 'Active Now',
      value: stats?.accounts?.active || 0,
      icon: Activity,
      color: 'text-green-500',
      bg: 'bg-green-500/10',
    },
    {
      title: 'Groups',
      value: stats?.groups?.total || 0,
      icon: Link2,
      color: 'text-purple-500',
      bg: 'bg-purple-500/10',
    },
    {
      title: 'Keywords',
      value: stats?.keywords?.total || 0,
      icon: KeyRound,
      color: 'text-orange-500',
      bg: 'bg-orange-500/10',
    },
    {
      title: 'Blocked Users',
      value: stats?.blocked_users || 0,
      icon: Users,
      color: 'text-red-500',
      bg: 'bg-red-500/10',
    },
    {
      title: 'Forwards Today',
      value: stats?.forwards_today || 0,
      icon: Send,
      color: 'text-cyan-500',
      bg: 'bg-cyan-500/10',
    },
  ]

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Dashboard</h1>
          <p className="text-muted-foreground text-sm">
            Real-time overview of your Telegram bot system
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div className={`h-2.5 w-2.5 rounded-full ${isConnected ? 'bg-green-500 animate-pulse' : 'bg-red-500'}`} />
          <span className="text-sm text-muted-foreground">
            {isConnected ? 'Live' : 'Offline'}
          </span>
        </div>
      </div>

      {/* Stats Grid */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6 gap-4">
        {statCards.map((card) => {
          const Icon = card.icon
          return (
            <Card key={card.title} className="hover:shadow-md transition-shadow">
              <CardContent className="p-4">
                <div className="flex items-center justify-between">
                  <div className={`p-2 rounded-lg ${card.bg}`}>
                    <Icon className={`h-5 w-5 ${card.color}`} />
                  </div>
                </div>
                <div className="mt-3">
                  <p className="text-2xl font-bold">{card.value}</p>
                  <p className="text-xs text-muted-foreground">{card.title}</p>
                </div>
              </CardContent>
            </Card>
          )
        })}
      </div>

      {/* Status Overview */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Account Status */}
        <Card>
          <CardHeader>
            <CardTitle className="text-lg flex items-center gap-2">
              <Smartphone className="h-5 w-5" />
              Account Status Distribution
            </CardTitle>
          </CardHeader>
          <CardContent>
            {stats?.accounts?.by_status ? (
              <div className="space-y-3">
                {Object.entries(stats.accounts.by_status).map(([status, count]: [string, any]) => {
                  const statusConfig: Record<string, { color: string; label: string }> = {
                    active: { color: 'bg-green-500', label: 'Active' },
                    pending: { color: 'bg-yellow-500', label: 'Pending' },
                    paused: { color: 'bg-gray-500', label: 'Paused' },
                    error: { color: 'bg-red-500', label: 'Error' },
                    banned: { color: 'bg-red-700', label: 'Banned' },
                    flood: { color: 'bg-orange-500', label: 'Flood' },
                    connecting: { color: 'bg-blue-500', label: 'Connecting' },
                  }
                  const config = statusConfig[status] || { color: 'bg-gray-400', label: status }
                  const total = stats.accounts.total || 1
                  const pct = Math.round((count / total) * 100)

                  return (
                    <div key={status} className="space-y-1">
                      <div className="flex justify-between text-sm">
                        <span className="capitalize">{config.label}</span>
                        <span className="text-muted-foreground">{count} ({pct}%)</span>
                      </div>
                      <div className="h-2 bg-muted rounded-full overflow-hidden">
                        <div
                          className={`h-full ${config.color} rounded-full transition-all`}
                          style={{ width: `${pct}%` }}
                        />
                      </div>
                    </div>
                  )
                })}
              </div>
            ) : (
              <div className="text-center text-muted-foreground py-8">
                <AlertTriangle className="h-8 w-8 mx-auto mb-2 opacity-50" />
                <p>No account data available</p>
              </div>
            )}
          </CardContent>
        </Card>

        {/* Tasks Overview */}
        <Card>
          <CardHeader>
            <CardTitle className="text-lg flex items-center gap-2">
              <MessageSquare className="h-5 w-5" />
              Task Queue Status
            </CardTitle>
          </CardHeader>
          <CardContent>
            {stats?.tasks ? (
              <div className="space-y-4">
                {Object.entries(stats.tasks).map(([status, count]: [string, any]) => {
                  const taskConfig: Record<string, { color: string; icon: any }> = {
                    pending: { color: 'text-yellow-500', icon: AlertTriangle },
                    processing: { color: 'text-blue-500', icon: Activity },
                    completed: { color: 'text-green-500', icon: Send },
                    failed: { color: 'text-red-500', icon: AlertTriangle },
                  }
                  const config = taskConfig[status] || { color: 'text-gray-500', icon: AlertTriangle }
                  const Icon = config.icon

                  return (
                    <div key={status} className="flex items-center justify-between p-3 bg-muted/50 rounded-lg">
                      <div className="flex items-center gap-3">
                        <Icon className={`h-5 w-5 ${config.color}`} />
                        <span className="capitalize font-medium">{status}</span>
                      </div>
                      <span className="text-lg font-bold">{count}</span>
                    </div>
                  )
                })}
              </div>
            ) : (
              <div className="text-center text-muted-foreground py-8">
                <p>No task data available</p>
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Quick Actions */}
      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Quick Tips</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4 text-sm text-muted-foreground">
            <div className="p-3 bg-muted/50 rounded-lg">
              <strong className="text-foreground block mb-1">Add Accounts</strong>
              Use the Telegram bot to add accounts with /addaccount command
            </div>
            <div className="p-3 bg-muted/50 rounded-lg">
              <strong className="text-foreground block mb-1">Monitor Groups</strong>
              Add groups from the Groups page to start monitoring
            </div>
            <div className="p-3 bg-muted/50 rounded-lg">
              <strong className="text-foreground block mb-1">Configure Keywords</strong>
              Set up keywords to trigger auto-replies and forwarding
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
