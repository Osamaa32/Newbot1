import { useEffect, useState } from 'react'
import { Settings, RotateCw, Save, ToggleLeft, ToggleRight } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { useApi } from '@/hooks/useApi'
import { useToast } from '@/components/ui/use-toast'
import { Toaster } from '@/components/ui/toaster'

interface Setting {
  id: number
  key: string
  value: string
  description?: string
}

const importantSettings = [
  'fallback_group_id',
  'rate_limit_max',
  'rate_limit_window',
  'default_auto_reply',
  'word_count_limit',
  'auto_reply_enabled',
  'forward_enabled',
  'join_delay_base',
  'join_delay_random',
  'cb_failure_threshold',
  'cb_recovery_timeout',
]

const booleanSettings = [
  'filter_mention',
  'filter_links',
  'filter_digits',
  'filter_private',
  'filter_outgoing',
  'filter_bots',
  'filter_admins',
  'auto_reply_enabled',
  'forward_enabled',
]

export default function SettingsPage() {
  const [settings, setSettings] = useState<Setting[]>([])
  const [editing, setEditing] = useState<Record<string, string>>({})
  const { get, put, loading } = useApi()
  const { toast } = useToast()

  const fetchSettings = async () => {
    try {
      const data = await get('/api/settings')
      setSettings(data.settings || [])
    } catch {
      toast({ title: 'Error', description: 'Failed to load settings', variant: 'destructive' })
    }
  }

  useEffect(() => {
    fetchSettings()
  }, [])

  const handleSave = async (key: string) => {
    try {
      await put('/api/settings', { key, value: editing[key] })
      toast({ title: 'Success', description: 'Setting updated' })
      setSettings(prev => prev.map(s => s.key === key ? { ...s, value: editing[key] } : s))
    } catch (e: any) {
      toast({ title: 'Error', description: e.message, variant: 'destructive' })
    }
  }

  const handleToggle = async (key: string) => {
    const current = settings.find(s => s.key === key)
    if (!current) return
    const newValue = current.value === 'true' ? 'false' : 'true'
    try {
      await put('/api/settings', { key, value: newValue })
      toast({ title: 'Success', description: 'Setting toggled' })
      setSettings(prev => prev.map(s => s.key === key ? { ...s, value: newValue } : s))
    } catch (e: any) {
      toast({ title: 'Error', description: e.message, variant: 'destructive' })
    }
  }

  const filteredSettings = settings.filter(s => importantSettings.includes(s.key))

  return (
    <div className="space-y-4">
      <Toaster />
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <Settings className="h-6 w-6" />
            Settings
          </h1>
          <p className="text-muted-foreground text-sm">Configure bot behavior</p>
        </div>
        <Button onClick={fetchSettings} variant="outline" size="icon">
          <RotateCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
        </Button>
      </div>

      <div className="grid gap-4">
        {filteredSettings.map((setting) => {
          const isBool = booleanSettings.includes(setting.key)
          const currentVal = editing[setting.key] !== undefined ? editing[setting.key] : setting.value

          return (
            <Card key={setting.key}>
              <CardContent className="p-4">
                <div className="flex items-center justify-between gap-4">
                  <div className="flex-1 min-w-0">
                    <p className="font-medium text-sm">{setting.key}</p>
                    <p className="text-xs text-muted-foreground truncate">
                      {setting.description || 'No description'}
                    </p>
                  </div>

                  {isBool ? (
                    <Button
                      variant="ghost"
                      size="icon"
                      onClick={() => handleToggle(setting.key)}
                    >
                      {setting.value === 'true' ? (
                        <ToggleRight className="h-6 w-6 text-green-500" />
                      ) : (
                        <ToggleLeft className="h-6 w-6 text-gray-400" />
                      )}
                    </Button>
                  ) : (
                    <div className="flex items-center gap-2">
                      <Input
                        value={currentVal}
                        onChange={(e) => setEditing(prev => ({ ...prev, [setting.key]: e.target.value }))}
                        className="w-48 text-sm"
                      />
                      {currentVal !== setting.value && (
                        <Button size="sm" onClick={() => handleSave(setting.key)}>
                          <Save className="h-4 w-4" />
                        </Button>
                      )}
                    </div>
                  )}
                </div>
              </CardContent>
            </Card>
          )
        })}
      </div>
    </div>
  )
}
