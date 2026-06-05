import { useEffect, useState } from 'react'
import { KeyRound, Plus, Trash2, RotateCw, Tag } from 'lucide-react'
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

interface Keyword {
  id: number
  word: string
  category: string
  is_active: boolean
  match_count: number
  created_at: string
}

export default function Keywords() {
  const [keywords, setKeywords] = useState<Keyword[]>([])
  const [newWord, setNewWord] = useState('')
  const [category, setCategory] = useState('general')
  const { get, post, del, loading } = useApi()
  const { toast } = useToast()

  const fetchKeywords = async () => {
    try {
      const data = await get('/api/keywords')
      setKeywords(data.keywords || [])
    } catch {
      toast({ title: 'Error', description: 'Failed to load keywords', variant: 'destructive' })
    }
  }

  useEffect(() => {
    fetchKeywords()
  }, [])

  const handleAdd = async () => {
    if (!newWord.trim()) return
    try {
      await post('/api/keywords', { word: newWord.trim(), category })
      toast({ title: 'Success', description: 'Keyword added' })
      setNewWord('')
      fetchKeywords()
    } catch (e: any) {
      toast({ title: 'Error', description: e.message, variant: 'destructive' })
    }
  }

  const handleDelete = async (id: number) => {
    try {
      await del(`/api/keywords/${id}`)
      toast({ title: 'Success', description: 'Keyword deleted' })
      fetchKeywords()
    } catch (e: any) {
      toast({ title: 'Error', description: e.message, variant: 'destructive' })
    }
  }

  const categories = [...new Set(keywords.map(k => k.category))]

  return (
    <div className="space-y-4">
      <Toaster />
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Keywords</h1>
          <p className="text-muted-foreground text-sm">Words that trigger bot actions</p>
        </div>
        <Button onClick={fetchKeywords} variant="outline" size="icon">
          <RotateCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
        </Button>
      </div>

      <Card>
        <CardHeader>
          <div className="flex gap-2">
            <Input
              placeholder="Enter keyword..."
              value={newWord}
              onChange={(e) => setNewWord(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleAdd()}
              className="flex-1"
            />
            <select
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              className="px-3 py-2 rounded-md border bg-background text-sm"
            >
              {categories.map(c => (
                <option key={c} value={c}>{c}</option>
              ))}
              <option value="general">general</option>
              <option value="urgent">urgent</option>
              <option value="custom">custom</option>
            </select>
            <Button onClick={handleAdd} disabled={!newWord.trim()}>
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
                  <TableHead>Word</TableHead>
                  <TableHead>Category</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Matches</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {keywords.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={5} className="text-center text-muted-foreground py-8">
                      No keywords configured
                    </TableCell>
                  </TableRow>
                ) : (
                  keywords.map((kw) => (
                    <TableRow key={kw.id}>
                      <TableCell>
                        <div className="flex items-center gap-2">
                          <KeyRound className="h-4 w-4 text-muted-foreground" />
                          <span className="font-medium">{kw.word}</span>
                        </div>
                      </TableCell>
                      <TableCell>
                        <Badge variant="outline" className="text-xs">
                          <Tag className="h-3 w-3 mr-1" />
                          {kw.category}
                        </Badge>
                      </TableCell>
                      <TableCell>
                        <Badge variant={kw.is_active ? 'default' : 'secondary'}>
                          {kw.is_active ? 'Active' : 'Inactive'}
                        </Badge>
                      </TableCell>
                      <TableCell>{kw.match_count}</TableCell>
                      <TableCell className="text-right">
                        <Button
                          variant="ghost" size="icon"
                          onClick={() => handleDelete(kw.id)}
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
          <p className="text-sm text-muted-foreground mt-2">
            Total: {keywords.length} keywords | Active: {keywords.filter(k => k.is_active).length}
          </p>
        </CardContent>
      </Card>
    </div>
  )
}
