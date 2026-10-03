#!/usr/bin/env bash
# Restart the local red-api (dashboard on) on 127.0.0.1:8010.
cd "$(dirname "$0")/.."
PIDF=/tmp/red-api-dash.pid
[ -f $PIDF ] && kill "$(cat $PIDF)" 2>/dev/null
fuser -k 8010/tcp 2>/dev/null
sleep 1
set -a; . ./.env.local-dev; set +a
nohup .venv/bin/gunicorn -w 1 --threads 8 -b 127.0.0.1:8010 -p $PIDF main:app > /tmp/red-api-dash.log 2>&1 &
sleep 3
