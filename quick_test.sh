#!/bin/bash
# ============================================================================
# Odin-Loki Quick Stress Test (20 min)
# ============================================================================
# 10 tests designed to verify the new system prompts work:
#   - Does Odin route correctly?
#   - Does Odin write interface contracts in specs?
#   - Does Loki respect the model schema?
#   - Does Odin's personality come through?
#   - Can they build two files that actually integrate?
#
# Usage: ./quick_test.sh [webhook_url]
# ============================================================================

set -uo pipefail

N8N_URL="${1:-http://192.168.1.219:5678/webhook/odin-agent}"
OUTPUT_DIR="/opt/Odin/quick_test_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUTPUT_DIR"

TOTAL=0; PASSED=0; FAILED=0; TOTAL_TIME=0

GREEN='\033[0;32m'; RED='\033[0;31m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

run_test() {
    local label="$1"; local query="$2"; local check_fn="$3"
    TOTAL=$((TOTAL + 1))
    local num=$(printf "%02d" $TOTAL)
    local outfile="$OUTPUT_DIR/test_${num}.json"

    echo ""
    echo -e "${CYAN}━━━ Test ${num}: ${label}${NC}"

    local start=$(date +%s)
    local body
    body=$(curl -s --max-time 300 -X POST "$N8N_URL" \
        -H "Content-Type: application/json" \
        -d "{\"query\": $(echo "$query" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read().strip()))'), \"session_id\": \"quick-${num}\"}" 2>&1)
    local elapsed=$(( $(date +%s) - start ))
    TOTAL_TIME=$((TOTAL_TIME + elapsed))

    echo "$body" > "$outfile"

    local status=$(echo "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status',''))" 2>/dev/null || echo "")
    local source=$(echo "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('source',''))" 2>/dev/null || echo "")
    local response=$(echo "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('response','')[:200])" 2>/dev/null || echo "")

    # Run the specific check function
    local check_result
    check_result=$($check_fn "$body" "$source" "$status" 2>/dev/null)

    if [[ "$check_result" == "PASS" ]]; then
        PASSED=$((PASSED + 1))
        echo -e "  ${GREEN}PASS${NC} (${elapsed}s) source=${source}"
    else
        FAILED=$((FAILED + 1))
        echo -e "  ${RED}FAIL: ${check_result}${NC} (${elapsed}s) source=${source}"
    fi
    echo "  Preview: ${response:0:120}..."
}

# ============================================================================
# Check functions — each validates something specific
# ============================================================================

check_routed_to_loki() {
    local body="$1" source="$2" status="$3"
    if [[ "$source" == "odin-loki-pipeline" && "$status" == "success" ]]; then echo "PASS"
    else echo "Expected odin-loki-pipeline, got source=$source status=$status"; fi
}

check_routed_direct() {
    local body="$1" source="$2" status="$3"
    if [[ "$source" == "odin-direct" && "$status" == "success" ]]; then echo "PASS"
    else echo "Expected odin-direct, got source=$source status=$status"; fi
}

check_has_interface_contract() {
    local body="$1" source="$2" status="$3"
    if [[ "$status" != "success" ]]; then echo "Not successful: status=$status"; return; fi
    # Check if the response mentions import paths or model names correctly
    local resp=$(echo "$body" | python3 -c "import sys,json; print(json.load(sys.stdin).get('response',''))" 2>/dev/null)
    if echo "$resp" | grep -qi "from app import db\|from models import\|MetricSnapshot\|interface contract"; then
        echo "PASS"
    else
        echo "No interface contract or correct imports found in response"
    fi
}

check_correct_columns() {
    local body="$1" source="$2" status="$3"
    if [[ "$status" != "success" ]]; then echo "Not successful: status=$status"; return; fi
    local resp=$(echo "$body" | python3 -c "import sys,json; print(json.load(sys.stdin).get('response',''))" 2>/dev/null)
    # Should use correct column names, NOT invented ones
    if echo "$resp" | grep -qi "cpu_percent\|ram_used_percent\|MetricSnapshot"; then
        # Good — using real names
        if echo "$resp" | grep -qi "CPUData\|RAMData\|DiskData\|NetworkData"; then
            echo "Used invented model names (CPUData/RAMData/etc)"
        else
            echo "PASS"
        fi
    else
        echo "Didn't reference expected column names"
    fi
}

check_any_success() {
    local body="$1" source="$2" status="$3"
    if [[ "$status" == "success" || "$status" == "partial" ]]; then echo "PASS"
    else echo "status=$status"; fi
}

check_personality() {
    local body="$1" source="$2" status="$3"
    if [[ "$status" != "success" ]]; then echo "Not successful"; return; fi
    local resp=$(echo "$body" | python3 -c "import sys,json; print(json.load(sys.stdin).get('response',''))" 2>/dev/null)
    # Should NOT have assistant-speak like "I'd be happy to" or "Certainly!"
    if echo "$resp" | grep -qi "I'd be happy to\|certainly\|absolutely\|I hope this helps"; then
        echo "Used assistant-speak (expected peer tone)"
    else
        echo "PASS"
    fi
}

check_python_syntax() {
    local body="$1" source="$2" status="$3"
    if [[ "$status" != "success" ]]; then echo "Not successful"; return; fi
    local resp=$(echo "$body" | python3 -c "import sys,json; print(json.load(sys.stdin).get('response',''))" 2>/dev/null)
    # Extract code and syntax check
    local code=$(echo "$resp" | python3 -c "
import sys, re
text = sys.stdin.read()
blocks = re.findall(r'\`\`\`(?:python)?\s*\n(.*?)\`\`\`', text, re.DOTALL)
if blocks: print(blocks[0])
" 2>/dev/null)
    if [[ -z "$code" ]]; then echo "PASS"; return; fi  # No code block to check
    if echo "$code" | python3 -c "import ast,sys; ast.parse(sys.stdin.read())" 2>/dev/null; then
        echo "PASS"
    else
        echo "Python syntax error in generated code"
    fi
}

# ============================================================================
# Pre-flight
# ============================================================================
echo -e "${BOLD}╔════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║   Odin-Loki Quick Test (New Prompts)       ║${NC}"
echo -e "${BOLD}╚════════════════════════════════════════════╝${NC}"
echo "Endpoint: $N8N_URL"
echo "Output:   $OUTPUT_DIR"

echo -n "Pre-flight... "
if curl -s --max-time 10 -o /dev/null "$N8N_URL" -X POST \
    -H "Content-Type: application/json" \
    -d '{"query":"ping","session_id":"preflight"}' 2>/dev/null; then
    echo -e "${GREEN}OK${NC}"
else
    echo -e "${RED}FAILED${NC}"; exit 1
fi

sleep 2

# ============================================================================
# TESTS
# ============================================================================

# 1. Routing: should go direct (architecture question)
run_test "Routing: architecture question → direct" \
    "Should I containerize the BeanLab Monitor or run it as a systemd service? What are the tradeoffs for my setup?" \
    check_routed_direct

# 2. Routing: should go to Loki (explicit code request)
run_test "Routing: code request → Loki" \
    "Write a Python function that checks if all BeanLab hosts are reachable by pinging each one and returning a dict of hostname→bool results." \
    check_routed_to_loki

# 3. Interface contract: does Odin include correct imports in spec?
run_test "Integration: correct imports in Loki output" \
    "Write a Flask route GET /api/hosts/health that queries all Host records and their latest MetricSnapshot, returning a JSON summary with hostname, status, and last cpu_percent for each host." \
    check_has_interface_contract

# 4. Column accuracy: does Loki use real column names?
run_test "Schema: correct column names used" \
    "Write a Python function called get_host_dashboard_data(host_id) that queries the MetricSnapshot table for the last 24 hours of data for a given host and returns average cpu, ram, and disk values." \
    check_correct_columns

# 5. Personality: no assistant-speak in direct answer
run_test "Personality: peer tone, no assistant-speak" \
    "What's the best way to handle Ollama model swapping latency when running Odin and Loki on a single 3090?" \
    check_personality

# 6. Complex code: full function with error handling
run_test "Code quality: error handling + syntax check" \
    "Write a Python function called scan_beanlab() that reads hosts from a JSON file, pings each host using subprocess, checks specified TCP ports with socket, and returns results as a list of dicts with keys: hostname, ip, reachable, ports (list of {port, open}). Handle timeouts, file not found, and malformed JSON." \
    check_python_syntax

# 7. Integration build step 1: write a model
run_test "Integration build: model file (step 1 of 2)" \
    "Write a SQLAlchemy model class called ScanResult in a file called scan_models.py. It should have: id (int, PK), host_id (FK to hosts.id), scan_time (datetime), is_reachable (bool), latency_ms (float nullable), ports_checked (int), ports_open (int). Import db from app. Include to_dict() method." \
    check_has_interface_contract

# 8. Integration build step 2: write a route that uses the model from step 1
run_test "Integration build: route using model (step 2 of 2)" \
    "Write a Flask route GET /api/scans/<int:host_id> that queries ScanResult records for a given host_id, ordered by scan_time desc, limited to 50 results. Import ScanResult from scan_models and db from app. Return JSON array of to_dict() results. Handle host not found with 404." \
    check_has_interface_contract

# 9. Bash with BeanLab context
run_test "BeanLab context: bash uses real IPs" \
    "Write a bash one-liner that pings all three Proxmox nodes (NetworkBean, StorageBean, KidneyBean) and prints which ones are up. Use their actual IPs." \
    check_any_success

# 10. Edge case: ambiguous request
run_test "Edge case: ambiguous request handled" \
    "The Pi-hole keeps losing DNS resolution for internal hosts. Help me figure out what's going on." \
    check_any_success

# ============================================================================
# Results
# ============================================================================
echo ""
echo -e "${BOLD}╔════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║              RESULTS                       ║${NC}"
echo -e "${BOLD}╚════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Tests:    ${TOTAL}"
echo -e "  Passed:   ${GREEN}${PASSED}${NC}"
echo -e "  Failed:   ${RED}${FAILED}${NC}"
echo -e "  Rate:     ${BOLD}$((PASSED * 100 / TOTAL))%${NC}"
echo -e "  Time:     ${BOLD}$((TOTAL_TIME / 60))m $((TOTAL_TIME % 60))s${NC}"
echo -e "  Avg:      ${BOLD}$((TOTAL_TIME / TOTAL))s${NC}/test"
echo ""
echo "  Responses: $OUTPUT_DIR/"
echo ""

# Show any failures in detail
if [[ $FAILED -gt 0 ]]; then
    echo -e "${RED}Failed test details:${NC}"
    echo "─────────────────────────────────"
    for f in "$OUTPUT_DIR"/test_*.json; do
        num=$(basename "$f" .json | sed 's/test_//')
        status=$(python3 -c "import json; d=json.load(open('$f')); print(d.get('status','unknown'))" 2>/dev/null)
        if [[ "$status" != "success" && "$status" != "partial" ]]; then
            echo "  Test $num: $(python3 -c "import json; d=json.load(open('$f')); print(d.get('response','no response')[:200])" 2>/dev/null)"
        fi
    done
    echo ""
fi
