import { Outlet, useLocation, useNavigate } from 'react-router'
import { useEffect, useState } from 'react'
import {
  LayoutDashboard, Smartphone, Link2, KeyRound,
  Settings, FileText, Menu, ChevronLeft, ChevronRight,
  Bot, Bell, Shield
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { useWebSocket } from '@/hooks/useWebSocket'

const navItems = [
  { icon: LayoutDashboard, label: 'Dashboard', path: '/' },
  { icon: Smartphone, label: 'Accounts', path: '/accounts' },
  { icon: Link2, label: 'Groups', path: '/groups' },
  { icon: KeyRound, label: 'Keywords', path: '/keywords' },
  { icon: Settings, label: 'Settings', path: '/settings' },
  { icon: FileText, label: 'Logs', path: '/logs' },
]

export default function DashboardLayout() {
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const [mobileOpen, setMobileOpen] = useState(false)
  const location = useLocation()
  const navigate = useNavigate()
  const { stats, isConnected } = useWebSocket()

  useEffect(() => {
    const token = localStorage.getItem('dashboard_token')
    if (!token && location.pathname !== '/login') {
      navigate('/login')
    }
  }, [location, navigate])

  const handleLogout = () => {
    localStorage.removeItem('dashboard_token')
    navigate('/login')
  }

  return (
    <div className="flex h-screen bg-background">
      {/* Mobile Overlay */}
      {mobileOpen && (
        <div
          className="fixed inset-0 bg-black/50 z-40 lg:hidden"
          onClick={() => setMobileOpen(false)}
        />
      )}

      {/* Sidebar */}
      <aside
        className={cn(
          'fixed lg:static inset-y-0 left-0 z-50 bg-card border-r transition-all duration-300 flex flex-col',
          sidebarOpen ? 'w-64' : 'w-16',
          mobileOpen ? 'translate-x-0' : '-translate-x-full lg:translate-x-0'
        )}
      >
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b">
          <div className={cn('flex items-center gap-2', !sidebarOpen && 'lg:hidden')}>
            <Bot className="h-7 w-7 text-primary" />
            <span className="font-bold text-lg">Telegram Control</span>
          </div>
          <div className={cn('lg:hidden', sidebarOpen && 'lg:block hidden')}>
            <Button variant="ghost" size="icon" onClick={() => setMobileOpen(false)}>
              <ChevronLeft className="h-4 w-4" />
            </Button>
          </div>
        </div>

        {/* Navigation */}
        <nav className="flex-1 py-4 px-2 space-y-1 overflow-y-auto">
          {navItems.map((item) => {
            const Icon = item.icon
            const isActive = location.pathname === item.path
            return (
              <button
                key={item.path}
                onClick={() => {
                  navigate(item.path)
                  setMobileOpen(false)
                }}
                className={cn(
                  'w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors',
                  isActive
                    ? 'bg-primary text-primary-foreground'
                    : 'text-muted-foreground hover:bg-accent hover:text-foreground',
                  !sidebarOpen && 'lg:justify-center lg:px-2'
                )}
              >
                <Icon className="h-5 w-5 flex-shrink-0" />
                <span className={cn(!sidebarOpen && 'lg:hidden')}>{item.label}</span>
              </button>
            )
          })}
        </nav>

        {/* Footer */}
        <div className="p-4 border-t space-y-2">
          <div className={cn('flex items-center gap-2 text-xs text-muted-foreground', !sidebarOpen && 'lg:hidden')}>
            <div className={cn('h-2 w-2 rounded-full', isConnected ? 'bg-green-500' : 'bg-red-500')} />
            {isConnected ? 'Connected' : 'Disconnected'}
          </div>
          <Button
            variant="outline"
            size="sm"
            className={cn('w-full', !sidebarOpen && 'lg:hidden')}
            onClick={handleLogout}
          >
            <Shield className="h-4 w-4 mr-2" />
            Logout
          </Button>
        </div>
      </aside>

      {/* Main Content */}
      <div className="flex-1 flex flex-col overflow-hidden">
        {/* Top Bar */}
        <header className="h-14 border-b bg-card flex items-center justify-between px-4">
          <div className="flex items-center gap-2">
            <Button
              variant="ghost"
              size="icon"
              className="lg:hidden"
              onClick={() => setMobileOpen(true)}
            >
              <Menu className="h-5 w-5" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              className="hidden lg:flex"
              onClick={() => setSidebarOpen(!sidebarOpen)}
            >
              {sidebarOpen ? <ChevronLeft className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
            </Button>
          </div>
          <div className="flex items-center gap-3">
            {stats && (
              <div className="hidden sm:flex items-center gap-4 text-xs text-muted-foreground">
                <span>Accounts: <strong className="text-foreground">{stats.accounts?.total || 0}</strong></span>
                <span>Active: <strong className="text-green-500">{stats.accounts?.active || 0}</strong></span>
                <span>Groups: <strong className="text-foreground">{stats.groups?.total || 0}</strong></span>
              </div>
            )}
            <Button variant="ghost" size="icon" className="relative">
              <Bell className="h-5 w-5" />
              <span className="absolute top-1 right-1 h-2 w-2 bg-primary rounded-full" />
            </Button>
          </div>
        </header>

        {/* Page Content */}
        <main className="flex-1 overflow-y-auto p-4 lg:p-6">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
