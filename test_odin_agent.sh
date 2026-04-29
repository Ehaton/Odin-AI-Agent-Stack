#!/bin/bash
# test_odin_agent.sh — Quick test for the Odin-Loki n8n workflow
# Usage: ./test_odin_agent.sh "your question here"
#
# Update the URL below after importing the workflow into n8n.
# The webhook URL will be shown in the Webhook Trigger node settings.
# Format: http://192.168.1.219:5678/webhook/odin-agent

N8N_URL="${N8N_WEBHOOK_URL:-http://192.168.1.219:5678/webhook/odin-agent}"
QUERY="${1:-Write a Python script that monitors disk usage and sends an alert if any partition exceeds 90%}"

echo "=== Odin-Loki Agent Test ==="
echo "Endpoint: $N8N_URL"
echo "Query: $QUERY"
echo "---"
echo ""

RESPONSE=$(curl -s -X POST "$N8N_URL" \
  -H "Content-Type: application/json" \
  -d "{
    \"query\": \"$QUERY\",
    \"session_id\": \"test-$(date +%s)\"
  }")

echo "$RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$RESPONSE"

echo ""
echo "=== Metadata ==="
echo "$RESPONSE" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    m = d.get('metadata', {})
    print(f\"Pipeline: {m.get('pipeline', 'unknown')}\")
    print(f\"Retries:  {m.get('retries', 0)}\")
    print(f\"Valid:    {m.get('validated', 'unknown')}\")
    print(f\"Status:   {d.get('status', 'unknown')}\")
except: print('Could not parse metadata')
" 2>/dev/null
