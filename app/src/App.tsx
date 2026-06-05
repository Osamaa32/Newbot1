import { Routes, Route } from 'react-router'
import { ThemeProvider } from '@/components/ui/theme-provider'
import DashboardLayout from './layouts/DashboardLayout'
import Dashboard from './pages/Dashboard'
import Accounts from './pages/Accounts'
import Groups from './pages/Groups'
import Keywords from './pages/Keywords'
import Settings from './pages/Settings'
import Logs from './pages/Logs'
import Login from './pages/Login'

export default function App() {
  return (
    <ThemeProvider defaultTheme="dark" storageKey="telegram-dashboard-theme">
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route element={<DashboardLayout />}>
          <Route path="/" element={<Dashboard />} />
          <Route path="/accounts" element={<Accounts />} />
          <Route path="/groups" element={<Groups />} />
          <Route path="/keywords" element={<Keywords />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/logs" element={<Logs />} />
        </Route>
      </Routes>
    </ThemeProvider>
  )
}
