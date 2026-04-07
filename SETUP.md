# PolyTerm Setup Guide — iPhone (Termius) + PC/Laptop

## Step 1: Set Up Your PC

### Windows

1. **Install Python 3.10+**
   - Download from https://python.org/downloads
   - During install, CHECK "Add Python to PATH"

2. **Open PowerShell** and run:
   ```powershell
   # Clone the repo
   git clone https://github.com/bradywhealth-lab/NEW_OPENCLAGENCY.git
   cd NEW_OPENCLAGENCY
   git checkout claude/lightweight-trading-terminal-kg54s

   # Install
   pip install -e .

   # Set up config
   copy .env.example .env
   ```

3. **Enable SSH** (so you can connect from your phone):
   - Settings → Apps → Optional Features → Add a Feature → "OpenSSH Server"
   - Open Services (Win+R → services.msc) → find "OpenSSH SSH Server" → Start + set to Automatic
   - Find your PC's IP: run `ipconfig` and note the IPv4 address (e.g., 192.168.1.50)

### Mac

1. **Install Python 3.10+**:
   ```bash
   brew install python@3.12
   ```

2. **Clone and install**:
   ```bash
   git clone https://github.com/bradywhealth-lab/NEW_OPENCLAGENCY.git
   cd NEW_OPENCLAGENCY
   git checkout claude/lightweight-trading-terminal-kg54s
   pip3 install -e .
   cp .env.example .env
   ```

3. **Enable SSH**:
   - System Settings → General → Sharing → Remote Login → ON
   - Find your IP: run `ifconfig | grep "inet "` (use the 192.168.x.x one)

### Linux

```bash
git clone https://github.com/bradywhealth-lab/NEW_OPENCLAGENCY.git
cd NEW_OPENCLAGENCY
git checkout claude/lightweight-trading-terminal-kg54s
pip install -e .
cp .env.example .env
# SSH is usually already enabled. Find IP: hostname -I
```

---

## Step 2: Configure the Bot

Edit the `.env` file on your PC:

```bash
# === PAPER TRADING (start here to test) ===
POLY_PAPER_TRADING=true
POLY_PAPER_BALANCE=100

# === LIVE TRADING (when ready) ===
# POLY_PAPER_TRADING=false
# POLY_PRIVATE_KEY=0xYourPrivateKeyHere
# POLY_FUNDER_ADDRESS=0xYourPolygonAddressHere

# === COPY TRADING (optional) ===
# POLY_COPY_TARGET=0xWalletAddressToCopy
# POLY_COPY_ENABLED=true
# POLY_COPY_MULTIPLIER=0.5
# POLY_COPY_AUTO_EXECUTE=false
```

---

## Step 3: Connect from iPhone (Termius)

1. Open **Termius** on your iPhone
2. Tap **+** → **New Host**
3. Fill in:
   - **Alias**: PolyTerm
   - **Hostname**: your PC's IP address (e.g., 192.168.1.50)
   - **Username**: your PC username
   - **Password**: your PC password
4. Tap **Save**, then tap the host to connect

5. Once connected, run:
   ```bash
   cd NEW_OPENCLAGENCY
   python run.py
   ```

**Tip**: Use landscape mode on your iPhone for the best layout.

---

## Step 4: Using the Terminal

### Keybindings

| Key | Action |
|-----|--------|
| `f` | **Scan for edges** — runs all 4 strategy scanners NOW |
| `b` | Focus buy order entry |
| `s` | Focus sell order entry |
| `r` | Refresh positions |
| `Esc` | Clear order form |
| `q` | Quit |

### How the Bot Works

The bot automatically scans every 60 seconds for:

1. **Arbitrage** — Yes+No pairs that cost less than $1.00 combined (risk-free profit)
2. **Crypto mispricing** — Polymarket crypto bets vs real exchange prices
3. **Weather mispricing** — Polymarket weather bets vs NWS forecasts
4. **Event resolution** — Markets where the outcome is already known

When an edge is found:
- A notification pops up with the opportunity details
- The order form is pre-filled with the recommended trade
- You press Submit to execute (or ignore to pass)

### Position Sizing

The bot uses **quarter-Kelly criterion** to protect your $100:
- Never bets more than 15% of bankroll on one trade
- Never deploys more than 60% total
- Adjusts bet size based on edge size and confidence
- Minimum $1 bet

### Paper Trading First!

Start with `POLY_PAPER_TRADING=true` and run for a few days.
Watch how the bot finds edges and how paper trades resolve.
Only go live when you're confident in the signals.

---

## Step 5: Going Live ($100 Real Money)

1. **Get a Polygon wallet** with USDC:
   - Use MetaMask or similar
   - Bridge USDC to Polygon network
   - Fund with your $100

2. **Get your private key**:
   - MetaMask → Account Details → Export Private Key
   - NEVER share this with anyone

3. **Approve Polymarket**:
   - Go to polymarket.com and connect your wallet
   - This sets up the necessary token approvals

4. **Update `.env`**:
   ```
   POLY_PAPER_TRADING=false
   POLY_PRIVATE_KEY=0xYourActualKey
   POLY_FUNDER_ADDRESS=0xYourWalletAddress
   ```

5. **Restart**: `python run.py`

---

## Staying Connected (Optional)

To keep the bot running when you close Termius:

```bash
# Use screen or tmux
screen -S polyterm
python run.py
# Press Ctrl+A, then D to detach

# To reconnect later:
screen -r polyterm
```

This keeps the bot scanning even when your phone is off.

---

## Remote Access Outside Your Home Network

If you want to access your PC from outside your WiFi:

**Option A: Tailscale (easiest, free)**
1. Install Tailscale on your PC: https://tailscale.com
2. Install Tailscale on your iPhone
3. Both get a 100.x.x.x IP — use that IP in Termius

**Option B: Port forwarding**
1. Router settings → Port Forward port 22 to your PC's local IP
2. Use your public IP in Termius (find at whatismyip.com)
