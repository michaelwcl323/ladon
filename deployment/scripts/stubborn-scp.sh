#!/bin/bash

retry_limit=$1
shift

scp "$@"
exit_status=$?
while [ "$exit_status" -ne 0 ] && [ "$retry_limit" -gt 0 ]; do
  >&2 echo "scp failed. Retrying. Attempts left: $retry_limit"
  retry_limit=$((retry_limit - 1))
  sleep 2
  scp "$@"
  exit_status=$?
done
exit "$exit_status"
