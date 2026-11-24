#!/bin/bash
# Probe openmaps.gov.bc.ca (DataBC WFS) from this machine and record whether
# connections succeed, are refused, or time out. Background: #264 (connections
# from GitHub-hosted runners are sometimes refused).
#
#   usage: probe_openmaps.sh [samples] [interval_seconds]
#
# Prints one line per sample and, on GitHub Actions, writes a table to the step
# summary. It records, it does not test: it always exits 0.
# curl exit 7 = connection refused, 28 = timed out.
set -u

SAMPLES=${1:-1}
INTERVAL=${2:-20}
# a small GetFeature request (HEAD requests to this endpoint return 404).
# PROBE_URL overrides it (eg to test this script)
URL="${PROBE_URL:-https://openmaps.gov.bc.ca/geo/pub/wfs?service=WFS&version=2.0.0&request=GetFeature&typeName=WHSE_BASEMAPPING.UTMG_UTM_ZONES_SP&outputFormat=json&count=1}"
SUMMARY=${GITHUB_STEP_SUMMARY:-/dev/null}

# same IP lookup as bcgov/actions-openshift's runner reachability probe
RUNNER_IP=$(curl -sS --max-time 20 https://api.ipify.org 2>/dev/null || echo unknown)
echo "runner IP: ${RUNNER_IP}"
{
  echo "### openmaps.gov.bc.ca from ${RUNNER_IP}"
  echo ""
  echo "| time (UTC) | HTTP | server IP | connect (s) | total (s) | curl exit |"
  echo "|---|---|---|---|---|---|"
} >> "$SUMMARY"

failed=0
for i in $(seq 1 "$SAMPLES"); do
  out=$(curl -s -o /dev/null --max-time 60 \
    -w "%{http_code}|%{remote_ip}|%{time_connect}|%{time_total}" "$URL")
  rc=$?
  # split on | rather than spaces, so an empty server IP does not shift the fields
  IFS="|" read -r code ip connect total <<< "$out"
  now=$(date -u +%H:%M:%S)
  note=""
  [ "$rc" -eq 7 ] && note=" (connection refused)"
  [ "$rc" -eq 28 ] && note=" (timed out)"
  [ "$rc" -ne 0 ] && failed=$((failed + 1))
  echo "${now} http=${code} server=${ip:--} connect=${connect}s total=${total}s curl_exit=${rc}${note}"
  echo "| ${now} | ${code} | ${ip:--} | ${connect} | ${total} | ${rc}${note} |" >> "$SUMMARY"
  if [ "$i" -lt "$SAMPLES" ]; then
    sleep "$INTERVAL"
  fi
done

echo "failed: ${failed} of ${SAMPLES}" | tee -a "$SUMMARY"
exit 0
