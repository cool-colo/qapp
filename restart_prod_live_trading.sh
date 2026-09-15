#!/bin/bash

LOG_DIR=/data/flc/code/prod_quant/qapp/logs
LOG_FILE="$LOG_DIR/restart_$(date +%F).log"

exec >> "$LOG_FILE" 2>&1

echo "========== $(date '+%F %T') Restart begin =========="

SESSION=bigqmt_prod1
WORKDIR=/data/flc/code/prod_quant/qapp

tmux send-keys -t "$SESSION" C-c
sleep 15
tmux send-keys -t "$SESSION" "cd $WORKDIR && ./start_prod_live_trading.sh" Enter

echo "========== $(date '+%F %T') Restart end =========="
