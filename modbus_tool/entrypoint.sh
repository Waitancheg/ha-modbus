#!/bin/sh
if [ -x /init ]; then
  exec /init "$@"
fi
exec /run.sh
