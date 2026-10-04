#!/bin/bash
cd /home/ubuntu/paper-trading
set -a; source .env; set +a
export OANDA_ACCOUNT_ID=101-001-39749670-002
export BOT_ID=setups
export INSTRUMENTS=EUR_USD,USD_JPY
exec venv/bin/python3 oanda_paper_trading_setups.py
