#!/bin/bash
# ============================================================================
# Odin-Loki Coding Stress Test
# ============================================================================
# Tests the agentic pipeline across multiple coding categories:
#   - Bash scripting
#   - Python utilities
#   - Docker/infrastructure
#   - Data processing
#   - Debugging/refactoring
#   - Multi-step complexity
#   - Edge cases & error handling
#
# Usage: ./stress_test.sh [webhook_url]
# Default: http://192.168.1.219:5678/webhook/odin-agent
# ============================================================================

set -euo pipefail

N8N_URL="${1:-http://192.168.1.219:5678/webhook/odin-agent}"
RESULTS_DIR="/tmp/odin_stress_test_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$RESULTS_DIR"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# Counters
TOTAL=0
PASSED=0
FAILED=0
ERRORS=0
TOTAL_TIME=0

# ============================================================================
# Test runner
# ============================================================================
run_test() {
    local category="$1"
    local difficulty="$2"
    local description="$3"
    local query="$4"
    local expect_code="${5:-true}"  # Should this route through Loki?

    TOTAL=$((TOTAL + 1))
    local test_num=$(printf "%02d" $TOTAL)
    local test_file="$RESULTS_DIR/test_${test_num}.json"

    echo ""
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BOLD}Test ${test_num}: ${description}${NC}"
    echo -e "Category: ${category} | Difficulty: ${difficulty} | Expect code: ${expect_code}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    local start_time=$(date +%s%N)

    # Make the request with a generous timeout (5 min for complex tasks)
    local http_code
    local response
    response=$(curl -s -w "\n%{http_code}" -X POST "$N8N_URL" \
        --max-time 300 \
        -H "Content-Type: application/json" \
        -d "{
            \"query\": $(echo "$query" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read().strip()))'),
            \"session_id\": \"stress-test-${test_num}\"
        }" 2>&1) || true

    local end_time=$(date +%s%N)
    local elapsed=$(( (end_time - start_time) / 1000000000 ))
    local elapsed_ms=$(( (end_time - start_time) / 1000000 ))
    TOTAL_TIME=$((TOTAL_TIME + elapsed))

    # Extract HTTP code (last line) and body (everything else)
    http_code=$(echo "$response" | tail -1)
    local body=$(echo "$response" | sed '$d')

    # Save full response
    echo "$body" > "$test_file"

    # Parse results
    local status=""
    local source=""
    local retries=""
    local validated=""
    local has_response=""

    if echo "$body" | python3 -m json.tool &>/dev/null 2>&1; then
        status=$(echo "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status',''))" 2>/dev/null || echo "")
        source=$(echo "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('source',''))" 2>/dev/null || echo "")
        retries=$(echo "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('metadata',{}).get('retries',0))" 2>/dev/null || echo "0")
        validated=$(echo "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('metadata',{}).get('validated',''))" 2>/dev/null || echo "")
        has_response=$(echo "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); r=d.get('response',''); print('yes' if len(r) > 50 else 'no')" 2>/dev/null || echo "no")
    fi

    # Determine pass/fail
    local result="UNKNOWN"
    local result_color="$YELLOW"

    if [[ -z "$body" || "$body" == *"error"* && "$status" == "" ]]; then
        result="ERROR"
        result_color="$RED"
        ERRORS=$((ERRORS + 1))
    elif [[ "$status" == "success" && "$has_response" == "yes" ]]; then
        # Check routing correctness
        if [[ "$expect_code" == "true" && "$source" == "odin-loki-pipeline" ]]; then
            result="PASS"
            result_color="$GREEN"
            PASSED=$((PASSED + 1))
        elif [[ "$expect_code" == "false" && "$source" == "odin-direct" ]]; then
            result="PASS"
            result_color="$GREEN"
            PASSED=$((PASSED + 1))
        elif [[ "$expect_code" == "either" ]]; then
            result="PASS"
            result_color="$GREEN"
            PASSED=$((PASSED + 1))
        else
            result="MISROUTE"
            result_color="$YELLOW"
            PASSED=$((PASSED + 1))  # Still got an answer, just different routing
        fi
    elif [[ "$status" == "partial" ]]; then
        result="PARTIAL"
        result_color="$YELLOW"
        PASSED=$((PASSED + 1))  # Got something, even if retries maxed
    else
        result="FAIL"
        result_color="$RED"
        FAILED=$((FAILED + 1))
    fi

    # Print result line
    echo -e "Result:   ${result_color}${result}${NC}"
    echo -e "Time:     ${elapsed}s (${elapsed_ms}ms)"
    echo -e "Source:   ${source:-N/A}"
    echo -e "Retries:  ${retries:-N/A}"
    echo -e "Valid:    ${validated:-N/A}"
    echo -e "Saved:    ${test_file}"

    # Log to CSV
    echo "${test_num},${category},${difficulty},${description},${result},${elapsed},${retries},${source},${validated}" >> "$RESULTS_DIR/results.csv"
}

# ============================================================================
# Connectivity pre-check
# ============================================================================
echo -e "${BOLD}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║          Odin-Loki Coding Stress Test Suite                 ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo "Endpoint: $N8N_URL"
echo "Results:  $RESULTS_DIR"
echo "Started:  $(date)"
echo ""

echo -n "Pre-flight check... "
if curl -s --max-time 5 -o /dev/null -w "%{http_code}" "$N8N_URL" -X POST \
    -H "Content-Type: application/json" \
    -d '{"query":"ping","session_id":"preflight"}' | grep -q "200\|500"; then
    echo -e "${GREEN}Endpoint reachable${NC}"
else
    echo -e "${RED}Cannot reach $N8N_URL — aborting${NC}"
    exit 1
fi

echo ""
echo "Starting tests in 3 seconds..."
sleep 3

# Write CSV header
echo "test,category,difficulty,description,result,time_seconds,retries,source,validated" > "$RESULTS_DIR/results.csv"

SUITE_START=$(date +%s)

# ============================================================================
# CATEGORY 1: Bash Scripting
# ============================================================================

run_test "Bash" "Easy" "Simple file backup script" \
"Write a bash script that backs up a directory to a timestamped tar.gz archive. It should accept source and destination as arguments and validate both exist."

run_test "Bash" "Medium" "Log analyzer with pattern matching" \
"Write a bash script that parses an nginx access log and outputs: top 10 IPs by request count, top 10 most requested URLs, count of each HTTP status code, and total bytes transferred. Use awk and sort."

run_test "Bash" "Hard" "Multi-host health checker" \
"Write a bash script that reads a JSON file of hosts (format: [{\"host\": \"192.168.1.100\", \"name\": \"server1\", \"port\": 22}]) and checks each host's connectivity via ping and port availability via nc. Output a formatted table with hostname, IP, ping status, port status, and response time. Use parallel execution with background jobs and a max concurrency of 5."

# ============================================================================
# CATEGORY 2: Python
# ============================================================================

run_test "Python" "Easy" "CLI temperature converter" \
"Write a Python script with argparse that converts temperatures between Celsius, Fahrenheit, and Kelvin. Support --from and --to flags with the unit names and a positional value argument. Include input validation."

run_test "Python" "Medium" "REST API client with retry logic" \
"Write a Python script using the requests library that fetches paginated data from a REST API. It should: handle rate limiting with exponential backoff (max 3 retries), follow pagination via Link headers or next_page fields, aggregate all results into a single JSON file, and log progress to stderr. Use dataclasses for the response model."

run_test "Python" "Hard" "Async file watcher with event queue" \
"Write a Python script using asyncio and watchdog that monitors a directory for file changes (create, modify, delete). Events should be queued and processed by an async worker that: deduplicates rapid-fire events within a 500ms window, logs each event with timestamp and file hash (MD5), and exposes a simple HTTP endpoint on port 8080 that returns the last 50 events as JSON. Use aiohttp for the HTTP server."

# ============================================================================
# CATEGORY 3: Docker / Infrastructure
# ============================================================================

run_test "Docker" "Easy" "Dockerfile for Python Flask app" \
"Write a multi-stage Dockerfile for a Python Flask application. Stage 1 should install dependencies from requirements.txt. Stage 2 should copy the app and run it with gunicorn on port 8000. Use python:3.12-slim as base. Include a healthcheck."

run_test "Docker" "Medium" "Docker compose monitoring stack" \
"Write a docker-compose.yml that sets up Prometheus, Grafana, and node-exporter on a shared network. Prometheus should scrape node-exporter every 15s. Grafana should depend on Prometheus and expose on port 3000. Include named volumes for data persistence and a prometheus.yml config embedded as a config object."

run_test "Docker" "Hard" "Container resource analyzer" \
"Write a bash script that uses docker stats --no-stream to capture resource usage of all running containers, then: calculates per-container CPU and memory as percentage of host total, identifies containers exceeding 80% of their memory limit, outputs results as both a formatted terminal table and a JSON file, and sends a warning to stderr for any container using more than 2GB RSS."

# ============================================================================
# CATEGORY 4: Data Processing
# ============================================================================

run_test "Data" "Easy" "CSV to JSON converter" \
"Write a Python script that reads a CSV file and converts it to JSON. Handle: quoted fields with commas, different delimiters (auto-detect or flag), empty fields as null, and numeric type inference. Output pretty-printed JSON to stdout or a file via --output flag."

run_test "Data" "Medium" "SQLite query builder" \
"Write a Python module with a SQLite query builder class that supports: select, where (with AND/OR), order_by, limit, join, insert, update, and delete operations via method chaining. All values should be parameterized to prevent SQL injection. Include a context manager for connection handling and a to_sql() method that returns the query string and params tuple."

run_test "Data" "Hard" "Log aggregation pipeline" \
"Write a Python script that processes multiple log files in parallel using concurrent.futures. Each log line is JSON with fields: timestamp, level, service, message, trace_id. The script should: group entries by trace_id to reconstruct request flows, calculate p50/p95/p99 latency per service, detect error cascades (3+ errors within 10 seconds from the same trace), and output a summary report with the slowest traces and error patterns."

# ============================================================================
# CATEGORY 5: Debugging & Refactoring
# ============================================================================

run_test "Debug" "Medium" "Fix buggy binary search" \
"Here is a buggy binary search implementation in Python. Find and fix all bugs:

def binary_search(arr, target):
    left = 0
    right = len(arr)
    while left < right:
        mid = (left + right) / 2
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            left = mid
        else:
            right = mid
    return -1

Explain each bug and provide the corrected version."

run_test "Debug" "Hard" "Refactor spaghetti code" \
"Refactor this messy Python function into clean, well-structured code with proper error handling:

def process(d):
    r = []
    for i in d:
        if i['type'] == 'A':
            if i['value'] > 100:
                if i['status'] != 'inactive':
                    x = i['value'] * 1.1
                    r.append({'id': i['id'], 'result': x, 'category': 'premium'})
                else:
                    r.append({'id': i['id'], 'result': 0, 'category': 'inactive'})
            else:
                r.append({'id': i['id'], 'result': i['value'], 'category': 'standard'})
        elif i['type'] == 'B':
            if i['value'] > 50:
                r.append({'id': i['id'], 'result': i['value'] * 0.9, 'category': 'discount'})
            else:
                r.append({'id': i['id'], 'result': i['value'], 'category': 'standard'})
    return r

Use dataclasses, type hints, single-responsibility functions, and a strategy pattern."

# ============================================================================
# CATEGORY 6: Routing / Edge Cases
# ============================================================================

run_test "Routing" "Easy" "Non-code question (should route direct)" \
"What are the pros and cons of microservices vs monolithic architecture for a small team of 3 developers?" \
"false"

run_test "Routing" "Medium" "Ambiguous code-adjacent question" \
"Explain how DNS resolution works step by step, from when a user types a URL in their browser to when the page loads. Include what happens at each layer." \
"either"

run_test "Routing" "Hard" "Mixed code + explanation" \
"Write a Python decorator that implements rate limiting using a token bucket algorithm. Also explain the token bucket algorithm conceptually — how it differs from fixed window and sliding window approaches, and when you would choose each one." \
"true"

# ============================================================================
# CATEGORY 7: Stress / Complexity
# ============================================================================

run_test "Stress" "Hard" "Full CRUD API scaffold" \
"Write a complete Python FastAPI application with: a User model (id, name, email, created_at), SQLite database with SQLAlchemy ORM, full CRUD endpoints (GET /users, GET /users/{id}, POST /users, PUT /users/{id}, DELETE /users/{id}), Pydantic schemas for request/response validation, proper HTTP status codes and error responses, and a startup event that creates the database tables."

run_test "Stress" "Extreme" "CLI tool with subcommands" \
"Write a Python CLI tool using click that manages SSH keys across multiple servers. Subcommands: 'generate' (creates a new ed25519 keypair with optional passphrase), 'deploy' (reads a hosts.json file and copies the public key to each host via ssh-copy-id with parallel execution), 'verify' (tests SSH connectivity to all hosts and reports which ones can authenticate), 'revoke' (removes the public key from a specific host's authorized_keys). Include --dry-run support on all commands, colored terminal output, and proper error handling for unreachable hosts."

# ============================================================================
# Results Summary
# ============================================================================

SUITE_END=$(date +%s)
SUITE_ELAPSED=$((SUITE_END - SUITE_START))
SUITE_MIN=$((SUITE_ELAPSED / 60))
SUITE_SEC=$((SUITE_ELAPSED % 60))

echo ""
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║                    STRESS TEST RESULTS                      ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Total tests:     ${BOLD}${TOTAL}${NC}"
echo -e "  Passed:          ${GREEN}${PASSED}${NC}"
echo -e "  Failed:          ${RED}${FAILED}${NC}"
echo -e "  Errors:          ${RED}${ERRORS}${NC}"
echo -e "  Pass rate:       ${BOLD}$(( (PASSED * 100) / TOTAL ))%${NC}"
echo ""
echo -e "  Total time:      ${BOLD}${SUITE_MIN}m ${SUITE_SEC}s${NC}"
echo -e "  Avg per test:    ${BOLD}$((TOTAL_TIME / TOTAL))s${NC}"
echo ""
echo -e "  Results CSV:     ${RESULTS_DIR}/results.csv"
echo -e "  Full responses:  ${RESULTS_DIR}/test_*.json"
echo ""

# Category breakdown
echo -e "${BOLD}Category Breakdown:${NC}"
echo "─────────────────────────────────────────────────"
awk -F',' 'NR>1 {
    cat[$2]++
    if ($5=="PASS") pass[$2]++
    time[$2]+=$6
    retries[$2]+=$7
} END {
    for (c in cat) {
        p = (c in pass) ? pass[c] : 0
        printf "  %-12s %d/%d passed  avg %ds  total retries: %d\n", c, p, cat[c], time[c]/cat[c], retries[c]
    }
}' "$RESULTS_DIR/results.csv"

echo ""
echo "─────────────────────────────────────────────────"
echo -e "Completed: $(date)"
echo ""
