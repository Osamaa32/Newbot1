import { useEffect, useState } from 'react'
import {
  Smartphone, Play, Square, RotateCw, Trash2,
  Plus, Search, ChevronDown, ChevronUp
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'
import { useApi } from '@/hooks/useApi'
import { useToast } from '@/components/ui/use-toast'
import { Toaster } from '@/components/ui/toaster'

interface Account {
  id: number
  phone: string
  status: string
  mode: string
  target_group_id: number
  first_name?: string
  last_name?: string
  total_messages: number
  total_replies: number
  last_connected?: string
  last_error?: string
}

const statusColors: Record<string, string> = {
  active: 'bg-green-500/10 text-green-500 border-green-500/20',
  pending: 'bg-yellow-500/10 text-yellow-500 border-yellow-500/20',
  paused: 'bg-gray-500/10 text-gray-500 border-gray-500/20',
  error: 'bg-red-500/10 text-red-500 border-red-500/20',
  banned: 'bg-red-700/10 text-red-700 border-red-700/20',
  flood: 'bg-orange-500/10 text-orange-500 border-orange-500/20',
  connecting: 'bg-blue-500/10 text-blue-500 border-blue-500/20',
}

export default function Accounts() {
  const [accounts, setAccounts] = useState<Account[]>([])
  const [search, setSearch] = useState('')
  const [expandedRow, setExpandedRow] = useState<number | null>(null)
  const { get, post, del, loading } = useApi()
  const { toast } = useToast()

  const fetchAccounts = async () => {
    try {
      const data = await get('/api/accounts')
      setAccounts(data.accounts || [])
    } catch {
      toast({ title: 'Error', description: 'Failed to load accounts', variant: 'destructive' })
    }
  }

  useEffect(() => {
    fetchAccounts()
    const interval = setInterval(fetchAccounts, 10000)
    return () => clearInterval(interval)
  }, [])

  const handleAction = async (phone: string, action: 'start' | 'stop' | 'restart') => {
    try {
      await post(`/api/accounts/${phone}/${action}`)
      toast({ title: 'Success', description: `Account ${action}ed successfully` })
      fetchAccounts()
    } catch (e: any) {
      toast({ title: 'Error', description: e.message, variant: 'destructive' })
    }
  }

  const handleDelete = async (phone: string) => {
    if (!confirm(`Delete account ${phone}? This cannot be undone.`)) return
    try {
      await del(`/api/accounts/${phone}`)
      toast({ title: 'Success', description: 'Account deleted' })
      fetchAccounts()
    } catch (e: any) {
      toast({ title: 'Error', description: e.message, variant: 'destructive' })
    }
  }

  const filtered = accounts.filter(a =>
    a.phone.includes(search) ||
    a.status.includes(search) ||
    `${a.first_name} ${a.last_name}`.toLowerCase().includes(search.toLowerCase())
  )

  return (
    <div className="space-y-4">
      <Toaster />

      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Accounts</h1>
          <p className="text-muted-foreground text-sm">Manage your Telegram accounts</p>
        </div>
        <Button onClick={fetchAccounts} variant="outline" size="icon">
          <RotateCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
        </Button>
      </div>

      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center gap-4">
            <div className="relative flex-1 max-w-md">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
              <Input
                placeholder="Search accounts..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="pl-10"
              />
            </div>
            <Badge variant="outline">{filtered.length} accounts</Badge>
          </div>
        </CardHeader>
        <CardContent>
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Phone</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Mode</TableHead>
                  <TableHead>Messages</TableHead>
                  <TableHead>Replies</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {filtered.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={6} className="text-center text-muted-foreground py-8">
                      No accounts found
                    </TableCell>
                  </TableRow>
                ) : (
                  filtered.map((account) => (
                    <>
                      <TableRow
                        key={account.id}
                        className="cursor-pointer hover:bg-muted/50"
                        onClick={() => setExpandedRow(expandedRow === account.id ? null : account.id)}
                      >
                        <TableCell className="font-medium">
                          <div className="flex items-center gap-2">
                            <Smartphone className="h-4 w-4 text-muted-foreground" />
                            {account.phone}
                          </div>
                        </TableCell>
                        <TableCell>
                          <Badge variant="outline" className={statusColors[account.status] || ''}>
                            {account.status}
                          </Badge>
                        </TableCell>
                        <TableCell className="capitalize">{account.mode}</TableCell>
                        <TableCell>{account.total_messages}</TableCell>
                        <TableCell>{account.total_replies}</TableCell>
                        <TableCell className="text-right">
                          <div className="flex items-center justify-end gap-1">
                            {account.status === 'active' ? (
                              <Button
                                variant="ghost" size="icon"
                                onClick={(e) => { e.stopPropagation(); handleAction(account.phone, 'stop') }}
                              >
                                <Square className="h-4 w-4 text-yellow-500" />
                              </Button>
                            ) : (
                              <Button
                                variant="ghost" size="icon"
                                onClick={(e) => { e.stopPropagation(); handleAction(account.phone, 'start') }}
                              >
                                <Play className="h-4 w-4 text-green-500" />
                              </Button>
                            )}
                            <Button
                              variant="ghost" size="icon"
                              onClick={(e) => { e.stopPropagation(); handleAction(account.phone, 'restart') }}
                            >
                              <RotateCw className="h-4 w-4" />
                            </Button>
                            <Button
                              variant="ghost" size="icon"
                              onClick={(e) => { e.stopPropagation(); handleDelete(account.phone) }}
                            >
                              <Trash2 className="h-4 w-4 text-red-500" />
                            </Button>
                            {expandedRow === account.id ? (
                              <ChevronUp className="h-4 w-4 text-muted-foreground" />
                            ) : (
                              <ChevronDown className="h-4 w-4 text-muted-foreground" />
                            )}
                          </div>
                        </TableCell>
                      </TableRow>
                      {expandedRow === account.id && (
                        <TableRow>
                          <TableCell colSpan={6} className="bg-muted/30">
                            <div className="grid grid-cols-2 gap-4 text-sm p-2">
                              <div>
                                <span className="text-muted-foreground">Target Group:</span>
                                <span className="ml-2 font-mono">{account.target_group_id}</span>
                              </div>
                              <div>
                                <span className="text-muted-foreground">Name:</span>
                                <span className="ml-2">{account.first_name} {account.last_name}</span>
                              </div>
                              <div>
                                <span className="text-muted-foreground">Last Connected:</span>
                                <span className="ml-2">{account.last_connected || 'Never'}</span>
                              </div>
                              {account.last_error && (
                                <div className="col-span-2">
                                  <span className="text-muted-foreground">Last Error:</span>
                                  <span className="ml-2 text-red-500">{account.last_error}</span>
                                </div>
                              )}
                            </div>
                          </TableCell>
                        </TableRow>
                      )}
                    </>
                  ))
                )}
              </TableBody>
            </Table>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
