import { useState, useCallback } from 'react'

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'

async function apiFetch(path: string, options?: RequestInit) {
  const res = await fetch(`${API_URL}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || err.message || `HTTP ${res.status}`)
  }
  return res.json()
}

export function useApi() {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const get = useCallback(async (path: string) => {
    setLoading(true)
    setError(null)
    try {
      const data = await apiFetch(path)
      return data
    } catch (e: any) {
      setError(e.message)
      throw e
    } finally {
      setLoading(false)
    }
  }, [])

  const post = useCallback(async (path: string, body?: any) => {
    setLoading(true)
    setError(null)
    try {
      const data = await apiFetch(path, {
        method: 'POST',
        body: body ? JSON.stringify(body) : undefined,
      })
      return data
    } catch (e: any) {
      setError(e.message)
      throw e
    } finally {
      setLoading(false)
    }
  }, [])

  const del = useCallback(async (path: string) => {
    setLoading(true)
    setError(null)
    try {
      const data = await apiFetch(path, { method: 'DELETE' })
      return data
    } catch (e: any) {
      setError(e.message)
      throw e
    } finally {
      setLoading(false)
    }
  }, [])

  const put = useCallback(async (path: string, body?: any) => {
    setLoading(true)
    setError(null)
    try {
      const data = await apiFetch(path, {
        method: 'PUT',
        body: body ? JSON.stringify(body) : undefined,
      })
      return data
    } catch (e: any) {
      setError(e.message)
      throw e
    } finally {
      setLoading(false)
    }
  }, [])

  return { get, post, put, del, loading, error }
}
