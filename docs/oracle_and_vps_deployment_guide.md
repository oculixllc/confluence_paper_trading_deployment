# Deploying the paper-trading runner: Oracle Cloud Free Tier (primary) + VPS fallback

This guide walks through hosting `oanda_paper_trading_book2.py` on an
always-on server so it runs on a strict 15-minute cron schedule with no
dependency on your laptop being open, awake, or in one place.

Two paths are covered, in the order to try them:

1. **Oracle Cloud Free Tier** — permanently free, real cloud infrastructure.
   More setup friction (account verification), but $0 forever.
2. **VPS fallback** (Hetzner used as the example) — if Oracle's signup or
   free-tier capacity gives you trouble. ~$4-6/month, less friction, very
   standard.

Both end up in the same place: a bare Ubuntu server you SSH into, where you
install Python, copy the project files over, and add a cron entry. Once you
have a working shell, Part 3 onward is identical for either path.

---

## Part 1A: Oracle Cloud Free Tier — account and VM setup

### 1. Create an Oracle Cloud account

1. Go to https://www.oracle.com/cloud/free/
2. Click **Start for free**.
3. Fill in email, country, name. Verify your email (check spam folder — Oracle's verification emails are frequently delayed or filtered).
4. You'll be asked for a phone number (SMS verification) and a **credit card**. This is for identity verification only — Oracle does not charge the card for Always Free resources, but the card must be valid and in most cases the billing address country must match your account's home region.
5. Choose your **Home Region** carefully during signup. This cannot be changed later without opening a support ticket. Pick the region geographically closest to you (affects latency to Oanda's servers, which matters for a 15-minute-bar trading system reacting to prices).
6. Wait for account activation. This can take a few minutes or, occasionally, up to a day if Oracle's automated verification flags your card/region combination for manual review. If it's stuck longer than 24 hours, their live chat support can usually unstick it.

**Known friction point:** Oracle's free-tier VM shape (`VM.Standard.A1.Flex`, the Arm-based always-free compute) is sometimes reported as "Out of capacity" in popular regions. If you hit this, either:
   - Try a different availability domain within the same region (the create-instance screen lets you pick AD-1/2/3)
   - Try again later (capacity fluctuates)
   - Fall back to the older `VM.Standard.E2.1.Micro` shape (x86, also always-free, smaller but sufficient for this workload and generally has better availability)

### 2. Create a Virtual Cloud Network (VCN)

Oracle usually offers to create this automatically when you create your first instance. If you want to do it manually first:

1. In the Oracle Cloud Console, open the hamburger menu (top left) -> **Networking** -> **Virtual Cloud Networks**.
2. Click **Start VCN Wizard** -> **Create VCN with Internet Connectivity** -> **Start VCN Wizard**.
3. Name it something like `paper-trading-vcn`. Leave the default CIDR blocks as-is unless you have a specific reason not to.
4. Click **Next**, review, then **Create**. This provisions a VCN, an internet gateway, a NAT gateway, and public/private subnets automatically.

### 3. Create the compute instance

1. Hamburger menu -> **Compute** -> **Instances** -> **Create Instance**.
2. **Name**: `paper-trading-server`.
3. **Placement**: leave default, or pick a specific availability domain if you hit capacity issues (see above).
4. **Image and shape**:
   - Click **Edit** next to "Image and shape."
   - Image: select **Canonical Ubuntu** -- pick the most recent LTS (22.04 or 24.04).
   - Shape: click **Change Shape** -> **Ampere** -> `VM.Standard.A1.Flex`. Set **1 OCPU** and **6 GB memory** (comfortably inside the always-free allowance of 4 OCPUs / 24 GB total, which you can split across up to 4 instances if you ever want to).
   - If `A1.Flex` shows no capacity, switch to **Specialty and previous generation** -> `VM.Standard.E2.1.Micro` instead -- it's smaller (1 OCPU, 1 GB RAM) but adequate for this script, which is not compute-heavy per run.
5. **Networking**: select the VCN and subnet created in Part 1A.2 (or let it create a new one now). Make sure **"Assign a public IPv4 address"** is checked -- you need this to SSH in.
6. **Add SSH keys**:
   - If you don't have an SSH key pair yet, generate one on your Mac first (see Part 1A.4 below), then come back to this step and paste in the **public** key.
   - Or select "Generate a key pair for me" and download both the private and public key Oracle generates -- save the private key somewhere safe, you cannot re-download it later.
7. Leave boot volume settings as default (the free tier includes up to 200GB total boot volume storage, split across your instances).
8. Click **Create**. Provisioning takes 1-3 minutes.

### 4. Generate an SSH key pair (if you don't already have one)

On your Mac, in Terminal:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/oracle_paper_trading -C "paper-trading-oracle"
```

Press enter through the passphrase prompts (or set one if you prefer -- you'll need to type it each time you connect, or add it to `ssh-agent`).

This creates two files:
- `~/.ssh/oracle_paper_trading` (private key -- never share this)
- `~/.ssh/oracle_paper_trading.pub` (public key -- this is what you paste into Oracle's instance creation screen)

To view the public key to paste in:
```bash
cat ~/.ssh/oracle_paper_trading.pub
```

### 5. Open the firewall for SSH (and confirm outbound access)

Oracle's default security list usually already allows inbound SSH (port 22) and all outbound traffic -- outbound is what matters here, since this script only makes outbound calls to Oanda's API, nothing needs to accept inbound connections except your own SSH session.

To confirm/adjust:
1. Hamburger menu -> **Networking** -> **Virtual Cloud Networks** -> click your VCN -> click the subnet your instance is in -> click the **Default Security List**.
2. Under **Ingress Rules**, confirm there's a rule allowing TCP port 22 from `0.0.0.0/0` (or restrict this to your home/known IP for better security -- see Part 5 below).
3. Under **Egress Rules**, confirm there's a rule allowing all traffic out (`0.0.0.0/0`, all protocols) -- this is the default and is what lets the server reach Oanda's API.

### 6. Connect via SSH

Find your instance's public IP: Compute -> Instances -> click your instance -> the **Public IP Address** is shown on the instance details page.

```bash
ssh -i ~/.ssh/oracle_paper_trading ubuntu@<PUBLIC_IP>
```

(Default username for Ubuntu images on Oracle is `ubuntu`.)

First connection will ask you to confirm the host fingerprint -- type `yes`.

If this hangs or refuses the connection, double check: the security list ingress rule for port 22, that you're using the correct private key path, and that the instance has finished booting (check its state shows "Running" in the console).

---

## Part 1B: VPS fallback (Hetzner example) -- if Oracle gives you trouble

Use this instead of Part 1A if Oracle's signup verification stalls, or the free-tier shape shows no capacity repeatedly in your region.

### 1. Create a Hetzner account

1. Go to https://www.hetzner.com/cloud/
2. Sign up with email + password, verify email.
3. Add a payment method (credit card or PayPal). Hetzner bills monthly in arrears -- no charge until you've actually used the server for a billing period.

### 2. Create a server

1. In the Hetzner Cloud Console, click **New Project** (name it e.g. `paper-trading`), then **Add Server**.
2. **Location**: pick the datacenter closest to you (Germany, Finland, or US options available).
3. **Image**: Ubuntu 24.04.
4. **Type**: the cheapest shared vCPU option (`CX22` or similar, ~EUR4-5/month) is more than sufficient for this workload.
5. **SSH key**: click **Add SSH Key**, paste in the same public key from Part 1A.4 above (`cat ~/.ssh/oracle_paper_trading.pub` -- the same key pair works fine here, no need to generate a separate one).
6. Leave networking/firewall defaults (Hetzner allows all outbound by default, and you'll SSH in on port 22 same as above).
7. Click **Create & Buy Now**.

### 3. Connect via SSH

Hetzner shows the server's public IP immediately after creation.

```bash
ssh -i ~/.ssh/oracle_paper_trading root@<PUBLIC_IP>
```

(Hetzner's default user is `root`, unlike Oracle's `ubuntu` -- everything else below works the same, just drop `sudo` since you're already root.)

---

## Part 2: Server setup (identical for Oracle and Hetzner from here)

Once you have an SSH session open on your server:

### 1. Update the system

```bash
sudo apt update && sudo apt upgrade -y
```

### 2. Install Python and pip

```bash
sudo apt install -y python3 python3-pip python3-venv
python3 --version
```

Confirm it's 3.10 or newer.

### 3. Create a working directory and a virtual environment

```bash
mkdir -p ~/paper-trading
cd ~/paper-trading
python3 -m venv venv
source venv/bin/activate
```

(The `source venv/bin/activate` step needs to be re-run every time you open a new SSH session and want to work with this environment interactively -- the cron job itself will call the venv's Python directly, no activation needed there. More on that in Part 4.)

### 4. Install the required Python packages

```bash
pip install --upgrade pip
pip install requests pandas numpy
```

### 5. Copy your project files to the server

From your **Mac's** terminal (a new local tab, not the SSH session), use `scp`:

```bash
scp -i ~/.ssh/oracle_paper_trading \
  confluence_engine_book2.py \
  chart_patterns_book2.py \
  confluence_engine.py \
  backtest_confluence.py \
  backtest_book2.py \
  oanda_paper_trading_book2.py \
  journal_book2.py \
  ubuntu@<PUBLIC_IP>:~/paper-trading/
```

(Use `root@<PUBLIC_IP>` instead of `ubuntu@<PUBLIC_IP>` if you're on the Hetzner VPS.)

If you get a "Permission denied" or "No such file or directory" error, confirm you're running this from the local directory that actually contains those `.py` files, and that `~/paper-trading` already exists on the server (created in step 3 above).

### 6. Verify the files transferred correctly

Back in your SSH session:

```bash
cd ~/paper-trading
ls -la
```

You should see all seven `.py` files listed.

---

## Part 3: Set environment variables and do a manual test run

### 1. Create an environment file

```bash
cd ~/paper-trading
nano .env
```

Paste in (replace with your actual values):

```
OANDA_API_KEY=your-practice-api-token-here
OANDA_ACCOUNT_ID=your-practice-account-id-here
```

If you deployed the Cloudflare dashboard, also add:

```
DASHBOARD_URL=https://confluence-paper-journal.your-subdomain.workers.dev
DASHBOARD_TOKEN=the-same-token-you-set-with-wrangler-secret-put
```

Save and exit `nano`: `Ctrl+O`, `Enter`, `Ctrl+X`.

**Lock down permissions on this file** since it holds API credentials:

```bash
chmod 600 .env
```

### 2. Do a manual test run

```bash
cd ~/paper-trading
source venv/bin/activate
set -a; source .env; set +a
python3 oanda_paper_trading_book2.py
```

Watch the output. You're looking for a `bar_evaluated` JSON line with a recent
timestamp and a real EUR/USD close price -- that confirms the script reached
Oanda's practice API successfully, computed indicators, and evaluated a
signal. If `signal` is `null`, that's expected most of the time (no
qualifying setup that bar) -- it does NOT mean something is broken.

If you get an error instead:
- `ERROR: set OANDA_API_KEY and OANDA_ACCOUNT_ID...` -- the `.env` file wasn't
  sourced correctly. Re-run the `set -a; source .env; set +a` line and check
  for typos in the file.
- A `requests.exceptions` or connection error -- check the security list /
  firewall's egress rules (Part 1A.5) allow outbound HTTPS (port 443).
- An import error for `pandas`/`numpy`/`requests` -- confirm the venv is
  activated (`source venv/bin/activate`) before running.

### 3. Check the log file

```bash
cat paper_trading_log_book2.jsonl
cat paper_trading_state_book2.json
```

Both should exist and contain sensible content after the manual run above.

---

## Part 4: Set up the cron job

### 1. Find the venv's Python path

```bash
cd ~/paper-trading
source venv/bin/activate
which python3
```

This will print something like `/home/ubuntu/paper-trading/venv/bin/python3`
-- copy this exact path, you need it for the crontab entry (cron does not
know about your venv activation, so you must point it directly at the venv's
python binary).

### 2. Open the crontab editor

```bash
crontab -e
```

If asked to choose an editor the first time, pick `nano` (option 1 is
usually nano) unless you're comfortable with `vim`.

### 3. Add the cron entry

At the bottom of the file, add (replacing the path with your actual venv
python path from step 1, and adjusting the working directory path if
different):

```
2,17,32,47 * * * * cd /home/ubuntu/paper-trading && set -a && source .env && set +a && /home/ubuntu/paper-trading/venv/bin/python3 oanda_paper_trading_book2.py >> cron_book2.log 2>&1
```

This runs the script at :02, :17, :32, and :47 past every hour -- 2 minutes
after each 15-minute bar closes, giving Oanda's API time to have the
completed candle available.

Save and exit (`Ctrl+O`, `Enter`, `Ctrl+X` in nano).

### 4. Verify the cron job is registered

```bash
crontab -l
```

You should see the line you just added.

### 5. Wait and verify it actually fires

Wait until the next :02/:17/:32/:47 mark passes, then:

```bash
tail -20 ~/paper-trading/cron_book2.log
tail -5 ~/paper-trading/paper_trading_log_book2.jsonl
```

A fresh `bar_evaluated` entry with a timestamp matching the current 15-minute
bar confirms the cron job fired and ran successfully.

If nothing appears after 20 minutes:
```bash
grep CRON /var/log/syslog | tail -20
```
This shows whether cron even attempted to run the job -- if it's not there
at all, double-check the crontab syntax; if it ran but the script errored,
the redirect (`>> cron_book2.log 2>&1`) should have captured the error
inside `cron_book2.log` itself.

---

## Part 5: Hardening (optional but recommended)

Since this server holds live API credentials and runs unattended for weeks:

1. **Restrict SSH to your own IP** rather than `0.0.0.0/0`:
   - Oracle: edit the security list ingress rule for port 22, change source CIDR to `<your-home-IP>/32`. Find your current IP via `curl ifconfig.me` from your Mac.
   - Hetzner: Cloud Console -> your server -> **Firewalls** -> create a firewall rule restricting port 22 to your IP.
   - Caveat: if your home IP changes (common with residential ISPs), you'll need to update this rule to reconnect. Consider a dynamic DNS service or just widening the rule temporarily if you get locked out.
2. **Set up unattended security updates**:
   ```bash
   sudo apt install -y unattended-upgrades
   sudo dpkg-reconfigure --priority=low unattended-upgrades
   ```
3. **Never expose the `.env` file** anywhere public -- it's already `chmod 600` (Part 3.1), which is the main protection since this server isn't hosting a public-facing app.
4. **Set up a basic uptime check**: a free tool like UptimeRobot (https://uptimerobot.com) can ping a simple heartbeat you add to the script, or just periodically SSH in yourself and check `cron_book2.log` timestamps.

---

## Quick reference: the essential commands once everything's set up

```bash
# SSH in
ssh -i ~/.ssh/oracle_paper_trading ubuntu@<PUBLIC_IP>

# Check recent activity
tail -20 ~/paper-trading/paper_trading_log_book2.jsonl

# Check current state (open position, review-trigger status, etc.)
cat ~/paper-trading/paper_trading_state_book2.json

# View the local journal
cd ~/paper-trading && source venv/bin/activate && python3 journal_book2.py

# Check cron is still registered
crontab -l

# Check the server is up (from your Mac, no SSH needed)
ping <PUBLIC_IP>
```

---

## Cost summary

| Path | Monthly cost | Notes |
|---|---|---|
| Oracle Cloud Free Tier | $0 | Permanently free, not a trial. Requires valid card on file for verification only. |
| Hetzner CX22 | ~EUR4-5 (~$5-6) | Billed monthly, cancel anytime, no long-term commitment. |
