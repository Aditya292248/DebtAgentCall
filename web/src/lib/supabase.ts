import { createClient } from '@supabase/supabase-js'

// The browser only reads, with the anon key, through row level security.
const url = import.meta.env.VITE_SUPABASE_URL as string | undefined
const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY as string | undefined

export const supabaseConfigError =
  !url || !anonKey ? 'Set VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY in web/.env.local' : null

export const supabase = createClient(url ?? 'http://127.0.0.1:54321', anonKey ?? 'missing')
