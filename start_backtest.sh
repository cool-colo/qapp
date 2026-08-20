#!/bin/bash

set -euo pipefail

cd /data/flc/code/quant/qapp

source /home/fanglicheng/miniconda3/etc/profile.d/conda.sh
conda activate quant

export QMT_LOG_DIRECTORY=/data/flc/code/quant/qapp/logs
export QMT_USE_REDIS=1
export QMT_REDIS_HOST=r-2zekv1mdld2oijmaj5.redis.rds.aliyuncs.com
export QMT_REDIS_PASSWORD=CUZus#3MdtmNDe@8wm
export QMT_REDIS_USERNAME=default
export MYSQL_HOST=pc-2zey98jewo9dn8aa4.rwlb.rds.aliyuncs.com
export MYSQL_PORT=3306
export MYSQL_USER=quant_user_test
export MYSQL_PASSWORD=3Uo*A^%I-oXec
export MYSQL_DATABASE=quant_routine_test
export QMT_LOG_FILE_NAME=model_preds
export MODEL_SNAPSHOTS_ENABLED=on



# 其它变量全部写这里

exec python backtests/target_model_predictions/run_backtest.py \
	--start 2026-07-01 \
	--end 2026-07-30 \
	--all-stocks \
	--log-level INFO \
	--write-results \
	--report-dir /tmp/qapp_model_report_check \
	--benchmark-code 399300.SZ \
        --config /data/flc/code/quant/qapp/configs/strategy_86008933.yaml
