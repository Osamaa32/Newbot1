# Telegram Control Bot — Command Reference

> **Complete guide to all available commands**

---

## Getting Started

1. Start the bot with `/start`
2. Enter your admin password
3. Use the menu or type commands

---

## Command Categories

### Authentication

| Command | Description |
|---------|-------------|
| `/start` | Start the bot and authenticate |
| `/cancel` | Cancel current operation |

### Account Management

| Command | Description | Example |
|---------|-------------|---------|
| `/accounts` | View all accounts with status | — |
| `/addaccount` | Add account (interactive wizard) | `/addaccount` |
| `/startacc` | Start a stopped account | `/startacc +966501234567` |
| `/stopacc` | Stop an active account | `/stopacc +966501234567` |
| `/removeacc` | Remove account permanently | `/removeacc +966501234567` |
| `/restartacc` | Restart account | `/restartacc +966501234567` |
| `/setmode` | Change account mode | `/setmode +966501234567 forward` |
| `/setgroup` | Change target group | `/setgroup +966501234567 -1001234567890` |
| `/accountinfo` | Show account details | `/accountinfo +966501234567` |

**Modes:** `forward` — forward only | `reply` — auto-reply only | `both` — both | `self` — self-monitor

### Statistics & Monitoring

| Command | Description |
|---------|-------------|
| `/stats` | Full system statistics |
| `/status` | All accounts status |
| `/engine` | Engine health status |
| `/health` | System health check |
| `/logs` | Recent system logs |

### Keywords

| Command | Description | Example |
|---------|-------------|---------|
| `/keywords` | Open keywords menu | — |
| `/addkw` | Add keyword | `/addkw مساعدة` |
| `/delkw` | Delete keyword | `/delkw مساعدة` |
| `/listkw` | List all keywords | — |

### Groups

| Command | Description | Example |
|---------|-------------|---------|
| `/groups` | Open groups menu | — |
| `/addgroup` | Add group link | `/addgroup https://t.me/groupname` |
| `/delgroup` | Delete group | `/delgroup 5` |
| `/listgroups` | List all groups | — |
| `/joinall` | Join all groups with account | `/joinall +966501234567` |

### Settings

| Command | Description | Example |
|---------|-------------|---------|
| `/settings` | View all settings | — |
| `/set` | Update a setting | `/set rate_limit_max 6` |
| `/reset` | Reset to default | `/reset rate_limit_max` |

### Auto Reply

| Command | Description |
|---------|-------------|
| `/replyset` | Set auto-reply message |
| `/replyshow` | Show current auto-reply |

**Variables:** `{{first_name}}` `{{last_name}}` `{{username}}` `{{user_id}}`

### Security

| Command | Description | Example |
|---------|-------------|---------|
| `/block` | Block a user | `/block 123456789` |
| `/unblock` | Unblock a user | `/unblock 123456789` |
| `/blocked` | List blocked users | — |

### Filters

| Command | Description |
|---------|-------------|
| `/filters` | View filter settings |
| `/togglefilter` | Toggle a filter on/off |

**Available Filters:**
- `mention` — Ignore messages with @mentions
- `links` — Ignore messages with URLs
- `digits` — Ignore messages with numbers
- `private` — Ignore private chats
- `outgoing` — Ignore outgoing messages
- `bots` — Ignore bot messages
- `admins` — Ignore admin messages

### System

| Command | Description |
|---------|-------------|
| `/refresh` | Refresh configuration |
| `/restart` | Restart all accounts |
| `/backup` | Create configuration backup |
| `/menu` | Show main menu |
| `/help` | Show this help |

---

## Quick Tips

- **Menu Buttons** — Use the reply keyboard for quick actions
- **Inline Buttons** — Tap buttons below messages for controls
- **Account Status** — Green dot = active, Red = error
- **Settings** — All changes are saved automatically
- **Backup** — Regular backups recommended

---

## Web Dashboard

Access the dashboard at your API URL:
- Local: `http://localhost:3000`
- Production: Your Railway/Docker URL

### Dashboard Features
- Real-time statistics (WebSocket)
- Account management table
- Keyword & group management
- Settings panel
- System logs viewer

---

**Version 6.0** | Built with Python + React
