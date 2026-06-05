import { useEffect, useState } from 'react'
import { Link2, Plus, Trash2, RotateCw, Globe } from 'lucide-react'
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

interface Group {
  id: number
  group_link: string
  title?: string
  group_id?: number
  member_count?: number
  is_active: boolean
  created_at: string
}

export default function Groups() {
  const [groups, setGroups] = useState<Group[]>([])
  const [newGroup, setNewGroup] = useState('')
  const { get, post, del, loading } = useApi()
  const { toast } = useToast()

  const fetchGroups = async () => {
    try {
      const data = await get('/api/groups')
      setGroups(data.groups || [])
    } catch {
      toast({ title: 'Error', description: 'Failed to load groups', variant: 'destructive' })
    }
  }

  useEffect(() => {
    fetchGroups()
  }, [])

  const handleAdd = async () => {
    if (!newGroup.trim()) return
    try {
      await post('/api/groups', { group_link: newGroup.trim() })
      toast({ title: 'Success', description: 'Group added' })
      setNewGroup('')
      fetchGroups()
    } catch (e: any) {
      toast({ title: 'Error', description: e.message, variant: 'destructive' })
    }
  }

  const handleDelete = async (id: number) => {
    if (!confirm('Delete this group?')) return
    try {
      await del(`/api/groups/${id}`)
      toast({ title: 'Success', description: 'Group deleted' })
      fetchGroups()
    } catch (e: any) {
      toast({ title: 'Error', description: e.message, variant: 'destructive' })
    }
  }

  return (
    <div className="space-y-4">
      <Toaster />
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Groups</h1>
          <p className="text-muted-foreground text-sm">Manage monitored groups</p>
        </div>
        <Button onClick={fetchGroups} variant="outline" size="icon">
          <RotateCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
        </Button>
      </div>

      <Card>
        <CardHeader>
          <div className="flex gap-2">
            <Input
              placeholder="Enter group link..."
              value={newGroup}
              onChange={(e) => setNewGroup(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleAdd()}
            />
            <Button onClick={handleAdd} disabled={!newGroup.trim()}>
              <Plus className="h-4 w-4 mr-1" />
              Add
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Link</TableHead>
                  <TableHead>Title</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Added</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {groups.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={5} className="text-center text-muted-foreground py-8">
                      No groups added yet
                    </TableCell>
                  </TableRow>
                ) : (
                  groups.map((group) => (
                    <TableRow key={group.id}>
                      <TableCell>
                        <div className="flex items-center gap-2">
                          <Globe className="h-4 w-4 text-muted-foreground" />
                          <span className="font-mono text-sm">{group.group_link}</span>
                        </div>
                      </TableCell>
                      <TableCell>{group.title || '—'}</TableCell>
                      <TableCell>
                        <Badge variant={group.is_active ? 'default' : 'secondary'}>
                          {group.is_active ? 'Active' : 'Inactive'}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-sm text-muted-foreground">
                        {new Date(group.created_at).toLocaleDateString()}
                      </TableCell>
                      <TableCell className="text-right">
                        <Button
                          variant="ghost" size="icon"
                          onClick={() => handleDelete(group.id)}
                        >
                          <Trash2 className="h-4 w-4 text-red-500" />
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          </div>
          <p className="text-sm text-muted-foreground mt-2">Total: {groups.length} groups</p>
        </CardContent>
      </Card>
    </div>
  )
}
