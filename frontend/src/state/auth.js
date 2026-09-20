import { reactive } from 'vue'

const USER_KEY = 'buildrig:user'
const ACCOUNTS_KEY = 'buildrig:accounts'

function accounts() {
  try { return JSON.parse(localStorage.getItem(ACCOUNTS_KEY) || '[]') } catch { return [] }
}

export const auth = reactive({
  user: JSON.parse(localStorage.getItem(USER_KEY) || 'null'),
  register({ name, email, password }) {
    const rows = accounts()
    if (rows.some((row) => row.email.toLowerCase() === email.toLowerCase())) throw new Error('An account already exists for this email.')
    const user = { id: `user_${btoa(email.toLowerCase()).replace(/[^a-z0-9]/gi, '').slice(0, 22)}`, name, email }
    rows.push({ ...user, password })
    localStorage.setItem(ACCOUNTS_KEY, JSON.stringify(rows))
    this.setUser(user)
  },
  login({ email, password }) {
    const account = accounts().find((row) => row.email.toLowerCase() === email.toLowerCase() && row.password === password)
    if (!account) throw new Error('Email or password is incorrect.')
    this.setUser({ id: account.id, name: account.name, email: account.email })
  },
  setUser(user) {
    this.user = user
    localStorage.setItem(USER_KEY, JSON.stringify(user))
  },
  logout() {
    this.user = null
    localStorage.removeItem(USER_KEY)
  }
})
