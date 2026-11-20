export type Theme = 'dark' | 'light'

const KEY = 'debtagentcall-theme'

export function loadTheme(): Theme {
  try {
    // Dark unless the person chose light on an earlier visit.
    return localStorage.getItem(KEY) === 'light' ? 'light' : 'dark'
  } catch {
    return 'dark'
  }
}

export function applyTheme(theme: Theme): void {
  document.documentElement.classList.toggle('dark', theme === 'dark')
  try {
    localStorage.setItem(KEY, theme)
  } catch {
    // Storage can be blocked; the theme still applies for this visit.
  }
}
