#!/bin/bash
# ============================================================================
# Odin-Loki Overnight Endurance Test
# ============================================================================
# ~7 hour run that:
#   Phase 1: Builds a BeanLab Monitoring Dashboard (Flask + SQLite)
#            incrementally — each step builds on the last
#   Phase 2: Independent stress tests woven between build phases
#   Phase 3: Syntax validation on all generated code
#
# The project is saved to $OUTPUT_DIR/beanlab-monitor/
# All logs, responses, and metrics go to $OUTPUT_DIR/
#
# Usage: ./overnight_stress.sh [webhook_url]
# Default: http://192.168.1.219:5678/webhook/odin-agent
# ============================================================================

set -uo pipefail

N8N_URL="${1:-http://192.168.1.219:5678/webhook/odin-agent}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_DIR="/opt/Odin/overnight_test_${TIMESTAMP}"
PROJECT_DIR="${OUTPUT_DIR}/beanlab-monitor"
LOGS_DIR="${OUTPUT_DIR}/logs"
RESPONSES_DIR="${OUTPUT_DIR}/responses"
CODE_DIR="${OUTPUT_DIR}/extracted_code"

mkdir -p "$PROJECT_DIR" "$LOGS_DIR" "$RESPONSES_DIR" "$CODE_DIR"

# Master log
MASTER_LOG="${OUTPUT_DIR}/master.log"
CSV_LOG="${OUTPUT_DIR}/results.csv"
SYNTAX_LOG="${OUTPUT_DIR}/syntax_checks.log"

# Counters
TOTAL=0
PASSED=0
FAILED=0
ERRORS=0
SYNTAX_PASS=0
SYNTAX_FAIL=0
BUILD_STEPS_COMPLETE=0
TOTAL_TIME_SEC=0

# Phase tracking
CURRENT_PHASE=""
PHASE_NUM=0

# ============================================================================
# Logging
# ============================================================================
log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
    echo "$msg" | tee -a "$MASTER_LOG"
}

log_header() {
    local msg="$1"
    echo "" | tee -a "$MASTER_LOG"
    echo "================================================================" | tee -a "$MASTER_LOG"
    echo "  $msg" | tee -a "$MASTER_LOG"
    echo "  $(date)" | tee -a "$MASTER_LOG"
    echo "================================================================" | tee -a "$MASTER_LOG"
}

# ============================================================================
# Code extraction from JSON response
# ============================================================================
extract_code() {
    local json_file="$1"
    local output_file="$2"
    local lang="${3:-python}"

    python3 << PYEOF
import json, re, sys

with open("${json_file}", "r") as f:
    try:
        data = json.load(f)
    except:
        sys.exit(1)

response = data.get("response", "")

# Extract all code blocks
patterns = [
    r'\`\`\`(?:python|py)\s*\n(.*?)\`\`\`',
    r'\`\`\`(?:bash|sh)\s*\n(.*?)\`\`\`',
    r'\`\`\`(?:html|jinja2?)\s*\n(.*?)\`\`\`',
    r'\`\`\`(?:css)\s*\n(.*?)\`\`\`',
    r'\`\`\`(?:javascript|js)\s*\n(.*?)\`\`\`',
    r'\`\`\`(?:yaml|yml)\s*\n(.*?)\`\`\`',
    r'\`\`\`(?:sql)\s*\n(.*?)\`\`\`',
    r'\`\`\`(?:dockerfile)\s*\n(.*?)\`\`\`',
    r'\`\`\`\s*\n(.*?)\`\`\`',
]

blocks = []
for pat in patterns:
    blocks.extend(re.findall(pat, response, re.DOTALL))

if blocks:
    with open("${output_file}", "w") as out:
        out.write("\n\n".join(blocks))
    print(f"Extracted {len(blocks)} code block(s)")
else:
    # Try the raw response if no code fences
    with open("${output_file}", "w") as out:
        out.write(response)
    print("No code blocks found, saved raw response")
PYEOF
}

# ============================================================================
# Syntax checker
# ============================================================================
check_syntax() {
    local file="$1"
    local lang="$2"
    local result="SKIP"

    case "$lang" in
        python)
            if python3 -c "
import ast, sys
try:
    with open('${file}') as f:
        ast.parse(f.read())
    sys.exit(0)
except SyntaxError as e:
    print(f'SyntaxError: {e}', file=sys.stderr)
    sys.exit(1)
" 2>>"$SYNTAX_LOG"; then
                result="PASS"
                SYNTAX_PASS=$((SYNTAX_PASS + 1))
            else
                result="FAIL"
                SYNTAX_FAIL=$((SYNTAX_FAIL + 1))
            fi
            ;;
        bash)
            if bash -n "$file" 2>>"$SYNTAX_LOG"; then
                result="PASS"
                SYNTAX_PASS=$((SYNTAX_PASS + 1))
            else
                result="FAIL"
                SYNTAX_FAIL=$((SYNTAX_FAIL + 1))
            fi
            ;;
        html|css|yaml|javascript|sql|dockerfile)
            result="SKIP"
            ;;
    esac

    echo "$result"
}

# ============================================================================
# Test runner
# ============================================================================
run_test() {
    local phase="$1"
    local category="$2"
    local difficulty="$3"
    local description="$4"
    local query="$5"
    local expect_code="${6:-true}"
    local save_as="${7:-}"        # Optional: filename to save extracted code
    local lang="${8:-python}"     # Language for syntax check
    local cooldown="${9:-15}"     # Seconds to wait after test (VRAM cooldown)

    TOTAL=$((TOTAL + 1))
    local test_num=$(printf "%03d" $TOTAL)
    local response_file="$RESPONSES_DIR/test_${test_num}.json"

    log "─── Test ${test_num}: ${description} [${category}/${difficulty}]"

    local start_time=$(date +%s)

    local response
    response=$(curl -s -w "\n%{http_code}" -X POST "$N8N_URL" \
        --max-time 300 \
        -H "Content-Type: application/json" \
        -d "{
            \"query\": $(echo "$query" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read().strip()))'),
            \"session_id\": \"overnight-${test_num}\"
        }" 2>&1) || true

    local end_time=$(date +%s)
    local elapsed=$((end_time - start_time))
    TOTAL_TIME_SEC=$((TOTAL_TIME_SEC + elapsed))

    local http_code=$(echo "$response" | tail -1)
    local body=$(echo "$response" | sed '$d')

    echo "$body" > "$response_file"

    # Parse
    local status="" source="" retries="" validated=""
    if echo "$body" | python3 -m json.tool &>/dev/null 2>&1; then
        status=$(echo "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status',''))" 2>/dev/null || echo "")
        source=$(echo "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('source',''))" 2>/dev/null || echo "")
        retries=$(echo "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('metadata',{}).get('retries',0))" 2>/dev/null || echo "0")
        validated=$(echo "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('metadata',{}).get('validated',''))" 2>/dev/null || echo "")
    fi

    # Result
    local result="FAIL"
    if [[ "$status" == "success" || "$status" == "partial" ]]; then
        result="PASS"
        PASSED=$((PASSED + 1))
    elif [[ -z "$body" || "$http_code" != "200" ]]; then
        result="ERROR"
        ERRORS=$((ERRORS + 1))
    else
        FAILED=$((FAILED + 1))
    fi

    # Extract and syntax-check code
    local syntax_result="N/A"
    if [[ -n "$save_as" && "$result" == "PASS" ]]; then
        local code_file="$CODE_DIR/${save_as}"
        extract_code "$response_file" "$code_file" "$lang" >> "$MASTER_LOG" 2>&1

        if [[ -f "$code_file" && -s "$code_file" ]]; then
            syntax_result=$(check_syntax "$code_file" "$lang")
            log "  Syntax check (${lang}): ${syntax_result}"

            # Also copy to project dir if it's a build step
            if [[ "$phase" == "BUILD" && -n "$save_as" ]]; then
                local proj_file="$PROJECT_DIR/${save_as}"
                mkdir -p "$(dirname "$proj_file")"
                cp "$code_file" "$proj_file"
                log "  Saved to project: ${save_as}"
            fi
        fi
    fi

    log "  Result: ${result} | Time: ${elapsed}s | Source: ${source:-N/A} | Retries: ${retries:-0} | Syntax: ${syntax_result}"

    # CSV log
    echo "${test_num},${phase},${category},${difficulty},\"${description}\",${result},${elapsed},${retries:-0},${source:-N/A},${validated:-N/A},${syntax_result},${save_as:-N/A}" >> "$CSV_LOG"

    # Cooldown between tests to let Ollama settle
    if [[ $cooldown -gt 0 ]]; then
        log "  Cooldown: ${cooldown}s"
        sleep "$cooldown"
    fi
}

# ============================================================================
# Health check with retry
# ============================================================================
health_check() {
    local attempt=0
    local max_attempts=5

    while [[ $attempt -lt $max_attempts ]]; do
        if curl -s --max-time 10 -o /dev/null "$N8N_URL" -X POST \
            -H "Content-Type: application/json" \
            -d '{"query":"ping","session_id":"health"}' 2>/dev/null; then
            return 0
        fi
        attempt=$((attempt + 1))
        log "Health check failed (attempt ${attempt}/${max_attempts}), retrying in 30s..."
        sleep 30
    done

    log "CRITICAL: Pipeline unreachable after ${max_attempts} attempts"
    return 1
}

# ============================================================================
# BEGIN
# ============================================================================

log_header "ODIN-LOKI OVERNIGHT ENDURANCE TEST"
log "Endpoint:    $N8N_URL"
log "Output:      $OUTPUT_DIR"
log "Project:     $PROJECT_DIR"
log "Start time:  $(date)"
log "Expected:    ~7 hours"
log ""

echo "test,phase,category,difficulty,description,result,time_seconds,retries,source,validated,syntax,saved_as" > "$CSV_LOG"

# Pre-flight
log "Running pre-flight health check..."
if ! health_check; then
    log "ABORT: Cannot reach Odin-Loki pipeline"
    exit 1
fi
log "Pre-flight PASSED"
sleep 5


# ============================================================================
# PHASE 1: PROJECT FOUNDATION (~45 min)
# ============================================================================
log_header "PHASE 1: PROJECT FOUNDATION"

run_test "BUILD" "Python" "Medium" \
    "BeanLab Monitor: Flask app skeleton with config" \
    "Write a Flask application skeleton for a homelab monitoring dashboard called 'BeanLab Monitor'. Requirements:
- app.py as the main entry point
- Config class with DEBUG, SECRET_KEY, DATABASE (SQLite at instance/beanlab.db), REFRESH_INTERVAL=30
- Blueprint structure ready for 'dashboard' and 'api' blueprints
- Flask-SQLAlchemy initialized
- Basic error handlers (404, 500)
- A create_app() factory function
- Include requirements.txt with: flask, flask-sqlalchemy, requests, psutil
Only output app.py and requirements.txt. Use Python 3.10+ features." \
    "true" "app.py" "python" 20

run_test "BUILD" "Python" "Medium" \
    "BeanLab Monitor: Database models" \
    "Write SQLAlchemy models for a homelab monitoring dashboard in a file called models.py. The models should track:

1. Host model: id, hostname, ip_address, description, host_type (proxmox/vm/lxc/docker/physical), status (online/offline/unknown), last_seen (datetime), cpu_cores, ram_total_gb, disk_total_gb, created_at

2. ServiceCheck model: id, host_id (FK to Host), service_name, port, protocol (tcp/udp/http), status (up/down/unknown), response_time_ms, last_checked, error_message

3. MetricSnapshot model: id, host_id (FK to Host), cpu_percent, ram_used_percent, disk_used_percent, network_in_bytes, network_out_bytes, timestamp

4. Alert model: id, host_id (FK to Host), alert_type (cpu_high/ram_high/disk_high/service_down/host_down), message, severity (info/warning/critical), acknowledged (boolean), created_at, acknowledged_at

Include relationships, __repr__, and a to_dict() method on each model. Use flask_sqlalchemy db instance imported from app." \
    "true" "models.py" "python" 20

run_test "BUILD" "Python" "Hard" \
    "BeanLab Monitor: Host scanner service" \
    "Write a Python module called scanner.py for the BeanLab monitoring dashboard. This service scans the homelab network and collects metrics. Requirements:

1. ping_host(ip): Uses subprocess to ping, returns (is_alive: bool, latency_ms: float)
2. check_port(ip, port, timeout=2): Uses socket to check TCP port availability
3. get_system_metrics(ip): For the LOCAL machine only (where this runs), use psutil to get CPU%, RAM%, disk%, network I/O
4. scan_host(host_dict): Takes a host dict with 'ip', 'hostname', 'services' (list of ports) and returns a full status report
5. scan_all_hosts(hosts_list): Runs scan_host on all hosts using concurrent.futures ThreadPoolExecutor with max_workers=10
6. A HostScanner class that wraps all of this with logging

The scanner should be importable by the Flask app and also runnable standalone for testing.
Include proper error handling for unreachable hosts and timeouts.
Use the BeanLab host list format: [{\"hostname\": \"NetworkBean\", \"ip\": \"192.168.1.206\", \"description\": \"Proxmox Node\", \"services\": [8006, 22]}]" \
    "true" "scanner.py" "python" 20


# ============================================================================
# STRESS INTERLUDE 1: Bash gauntlet (~30 min)
# ============================================================================
log_header "STRESS INTERLUDE 1: BASH GAUNTLET"

run_test "STRESS" "Bash" "Medium" \
    "Disk usage reporter with email alert" \
    "Write a bash script that checks disk usage on all mounted partitions. If any partition exceeds 85%, format a report with hostname, partition, usage%, and total/used/free space, then output it as both a terminal table and a JSON file. Include a --threshold flag to customize the percentage." \
    "true" "stress_disk_reporter.sh" "bash" 15

run_test "STRESS" "Bash" "Hard" \
    "Docker container log rotator" \
    "Write a bash script that manages Docker container logs. It should: find all container log files in /var/lib/docker/containers/, check each log file size, rotate any log over 100MB by compressing with gzip and adding a timestamp suffix, keep only the last 5 rotated logs per container, output a summary of actions taken, and support a --dry-run flag. Include proper error handling for permission issues." \
    "true" "stress_log_rotator.sh" "bash" 15

run_test "STRESS" "Bash" "Hard" \
    "SSL certificate expiry checker" \
    "Write a bash script that checks SSL certificate expiry dates for a list of domains. Read domains from a file (one per line) or accept them as arguments. For each domain: connect with openssl s_client, extract the expiry date, calculate days remaining, and flag certificates expiring within 30 days as WARNING and within 7 days as CRITICAL. Output as a sorted table (most urgent first) and optionally as JSON with --json flag." \
    "true" "stress_ssl_checker.sh" "bash" 15

run_test "STRESS" "Bash" "Extreme" \
    "Automated backup verification system" \
    "Write a bash script that verifies backup integrity across multiple backup locations. It should: read a config file (YAML-like key=value) listing backup paths and expected file patterns, check each location for: newest file age (alert if older than 24h), file count vs expected minimum, file size vs expected minimum, checksum verification for the latest backup (MD5), generate a pass/fail report per backup set, support --verbose for detailed output and --quiet for exit-code-only mode. Use associative arrays for the config." \
    "true" "stress_backup_verify.sh" "bash" 15


# ============================================================================
# PHASE 2: DASHBOARD ROUTES & TEMPLATES (~60 min)
# ============================================================================
log_header "PHASE 2: DASHBOARD ROUTES & TEMPLATES"

run_test "BUILD" "Python" "Medium" \
    "BeanLab Monitor: API routes" \
    "Write a Flask Blueprint called api_bp in a file called routes_api.py for the BeanLab monitoring dashboard API. Endpoints:

GET /api/hosts - List all hosts with optional ?status=online filter
GET /api/hosts/<id> - Get host details with latest metrics and service checks
POST /api/hosts - Add a new host (JSON body: hostname, ip_address, description, host_type)
PUT /api/hosts/<id> - Update host details
DELETE /api/hosts/<id> - Remove a host
GET /api/hosts/<id>/metrics - Get metrics history with ?hours=24 filter
GET /api/metrics/summary - Aggregate stats: total hosts, online count, avg CPU/RAM/disk across all
POST /api/scan - Trigger a manual scan of all hosts (calls scanner.scan_all_hosts)
GET /api/alerts - List alerts with ?severity=critical&acknowledged=false filters
PUT /api/alerts/<id>/acknowledge - Mark alert as acknowledged

All endpoints return JSON. Use proper HTTP status codes. Import models from models.py and scanner from scanner.py." \
    "true" "routes_api.py" "python" 20

run_test "BUILD" "HTML" "Hard" \
    "BeanLab Monitor: Dashboard HTML template" \
    "Write a Jinja2 HTML template called dashboard.html for the BeanLab monitoring dashboard. It should be a single-page dashboard with:

1. Header bar: 'BeanLab Monitor' title, last scan timestamp, a 'Scan Now' button
2. Summary cards row: Total Hosts (with online/offline counts), Average CPU%, Average RAM%, Active Alerts count (colored red if >0)
3. Host grid: Cards for each host showing hostname, IP, status badge (green/yellow/red), CPU/RAM/disk mini bar charts, service status dots, last seen time
4. Alerts panel: Scrollable list of recent alerts with severity badges and acknowledge buttons
5. Auto-refresh every 30 seconds via JavaScript fetch to /api/metrics/summary and /api/hosts

Use a bean-themed color palette: dark brown (#3E2723) header, cream (#FFF8E1) background, green (#4CAF50) for healthy, amber (#FF9800) for warning, red (#F44336) for critical.
Include embedded CSS (no external files). Use semantic HTML5 elements. Make it responsive with CSS grid.
The template should extend a base.html with blocks for title, content, and scripts." \
    "true" "templates/dashboard.html" "html" 20

run_test "BUILD" "HTML" "Medium" \
    "BeanLab Monitor: Base HTML template" \
    "Write a Jinja2 base template called base.html for the BeanLab Monitor Flask app. Include:
- HTML5 doctype with charset and viewport meta
- Title block defaulting to 'BeanLab Monitor'
- Embedded CSS with the bean theme: dark brown (#3E2723) nav, cream (#FFF8E1) body, system font stack
- A nav bar with 'BeanLab Monitor' brand, links to Dashboard, Hosts, Alerts
- A main content block
- A footer with 'BeanLab Monitor v1.0 | Powered by Odin & Loki'
- A scripts block at the bottom
- CSS grid-based layout, fully responsive" \
    "true" "templates/base.html" "html" 20

run_test "BUILD" "Python" "Medium" \
    "BeanLab Monitor: Dashboard routes" \
    "Write a Flask Blueprint called dashboard_bp in a file called routes_dashboard.py for the BeanLab monitoring dashboard views. Routes:

GET / - Redirect to /dashboard
GET /dashboard - Render dashboard.html with context: hosts, summary stats, recent alerts
GET /hosts - Render hosts.html listing all hosts in a table
GET /hosts/<id> - Render host_detail.html with full host info, metrics charts, service checks
GET /alerts - Render alerts.html with filterable alert list

Import models from models.py. Use SQLAlchemy queries with proper joins and aggregation where needed.
Include error handling for missing hosts (404)." \
    "true" "routes_dashboard.py" "python" 20


# ============================================================================
# STRESS INTERLUDE 2: Python deep dive (~45 min)
# ============================================================================
log_header "STRESS INTERLUDE 2: PYTHON DEEP DIVE"

run_test "STRESS" "Python" "Hard" \
    "Async port scanner with banner grabbing" \
    "Write a Python async port scanner using asyncio that: scans a given IP across a port range (default 1-1024), uses asyncio.open_connection with a 2-second timeout, grabs service banners (first 1024 bytes) from open ports, supports scanning multiple IPs concurrently with a semaphore limit of 100, outputs results as a formatted table and JSON file. Use argparse for CLI: --target, --ports (range like 1-1024), --timeout, --output." \
    "true" "stress_port_scanner.py" "python" 15

run_test "STRESS" "Python" "Hard" \
    "Event-driven state machine" \
    "Write a Python module implementing a generic finite state machine (FSM) with: a StateMachine class supporting add_state, add_transition, set_initial, trigger methods, support for guard conditions (functions that must return True for transition to fire), entry/exit callbacks on states, a history of state transitions with timestamps, serialization to/from JSON for persistence, type hints throughout, and a demo showing a deployment pipeline FSM with states: idle, building, testing, deploying, deployed, failed." \
    "true" "stress_state_machine.py" "python" 15

run_test "STRESS" "Python" "Extreme" \
    "Plugin system with dynamic loading" \
    "Write a Python plugin framework that: discovers and loads plugins from a directory, each plugin is a .py file with a class inheriting from BasePlugin, BasePlugin defines: name, version, setup(), teardown(), execute(context), a PluginManager handles: discovery, dependency ordering, lifecycle management, includes a PluginContext that plugins can read/write shared state to, supports plugin priority ordering and enable/disable, includes 3 example plugins: a logger, a metrics collector, and a notifier. Use importlib for dynamic loading and abc for the base class." \
    "true" "stress_plugin_system.py" "python" 15

run_test "STRESS" "Python" "Medium" \
    "Configuration manager with validation" \
    "Write a Python configuration manager that: loads config from YAML, JSON, env vars, and CLI args (in that priority order), validates config against a schema defined as a dataclass with type hints, supports nested config sections, provides dot-notation access (config.database.host), includes defaults, required field validation, and type coercion, thread-safe singleton pattern, and a dump() method showing final merged config with source attribution. Do not use pydantic — use dataclasses and manual validation." \
    "true" "stress_config_manager.py" "python" 15


# ============================================================================
# PHASE 3: BACKGROUND SERVICES (~45 min)
# ============================================================================
log_header "PHASE 3: BACKGROUND SERVICES"

run_test "BUILD" "Python" "Hard" \
    "BeanLab Monitor: Background scheduler" \
    "Write a Python module called scheduler.py for the BeanLab monitoring dashboard that runs background scanning tasks. Requirements:

1. Use threading (not APScheduler) to create a simple repeating task scheduler
2. ScanScheduler class with: start(), stop(), set_interval(seconds), is_running property
3. The scan loop should: call scanner.scan_all_hosts(), update Host and MetricSnapshot records in the database, create Alert records when thresholds are exceeded (CPU>90%, RAM>90%, disk>90%, host unreachable), auto-clear alerts when conditions return to normal
4. Alert thresholds should be configurable
5. Thread-safe database access using Flask app context
6. Graceful shutdown on SIGTERM/SIGINT
7. Logging of each scan cycle with timing

The scheduler should integrate with the Flask app factory pattern (init_scheduler(app) function)." \
    "true" "scheduler.py" "python" 20

run_test "BUILD" "Python" "Medium" \
    "BeanLab Monitor: Seed data script" \
    "Write a Python script called seed_data.py that populates the BeanLab monitoring database with the actual BeanLab hosts:

Hosts to add:
- NetworkBean: 192.168.1.206, Proxmox node, ports [8006, 22]
- StorageBean: 192.168.1.207, Proxmox node, ports [8006, 22]
- KidneyBean: 192.168.1.109, Proxmox node, ports [8006, 22]
- ai-stack-420: 192.168.1.111, VM (RTX 3090), ports [11434, 3000, 22, 8080]
- BeanNAS: 192.168.1.171, TrueNAS Scale, ports [80, 443, 22]
- Pi-hole: 192.168.1.228, DNS server, ports [80, 53]
- NGINX-PM: 192.168.1.154, Reverse proxy, ports [80, 443, 81]
- WireGuard: 192.168.1.220, VPN server, ports [51820]
- BookStack: 192.168.1.115, Wiki, ports [80, 443]
- RustDesk: 192.168.1.116, Remote desktop, ports [21115, 21116, 21117]
- Jellyfin: 192.168.1.55, Media server, ports [8096]
- NextCloud: 192.168.1.77, Cloud storage, ports [80, 443]
- Windows-DC: 192.168.1.199, Domain controller, ports [3389, 445, 53, 389]

Use the Flask app context. Generate some fake historical MetricSnapshot data (random but realistic values over the past 24 hours, one snapshot per host every 5 minutes) using random. Also create a few sample alerts." \
    "true" "seed_data.py" "python" 20

run_test "BUILD" "Python" "Medium" \
    "BeanLab Monitor: WSGI entry point and run script" \
    "Write two files for the BeanLab monitoring dashboard:

1. wsgi.py: A WSGI entry point that creates the Flask app using create_app(), initializes the database (db.create_all()), registers blueprints (dashboard_bp, api_bp), initializes the scanner scheduler, and is suitable for running with gunicorn.

2. run.sh: A bash script that sets up and runs the BeanLab Monitor. It should:
- Check for Python 3.10+
- Create a venv if not exists
- Install requirements from requirements.txt
- Initialize the database with seed data (only if beanlab.db doesn't exist)
- Start the app with gunicorn on 0.0.0.0:5555 with 2 workers
- Support a --dev flag to run with Flask's dev server instead
- Trap SIGTERM for clean shutdown

Both files should import from the project's modules (app, models, scanner, scheduler, routes)." \
    "true" "wsgi.py" "python" 20


# ============================================================================
# STRESS INTERLUDE 3: Docker & infrastructure (~30 min)
# ============================================================================
log_header "STRESS INTERLUDE 3: DOCKER & INFRASTRUCTURE"

run_test "STRESS" "Docker" "Medium" \
    "Dockerfile for the BeanLab Monitor" \
    "Write a production Dockerfile for a Flask monitoring dashboard. Multi-stage build with python:3.12-slim. Stage 1: install deps from requirements.txt. Stage 2: copy app, create non-root user, expose port 5555, healthcheck via curl to localhost:5555/api/metrics/summary, run with gunicorn. Include .dockerignore contents as a comment at the top." \
    "true" "stress_dockerfile" "dockerfile" 15

run_test "STRESS" "Docker" "Hard" \
    "Docker Compose for full monitoring stack" \
    "Write a docker-compose.yml that deploys: the BeanLab Monitor Flask app (build from local Dockerfile), a Redis container for caching (optional future use), and a Watchtower container for auto-updates. All on a shared beanlab-net network. The Flask app should have environment variables for DATABASE_URL, SECRET_KEY, SCAN_INTERVAL. Include named volumes for the SQLite database and Redis data. Add restart policies and resource limits." \
    "true" "stress_compose.yml" "yaml" 15

run_test "STRESS" "Bash" "Hard" \
    "Systemd service generator" \
    "Write a bash script that generates a systemd service unit file for any given application. Accept arguments: --name (service name), --exec (command to run), --user, --workdir, --env (repeatable, KEY=VALUE), --restart (always/on-failure/no), --after (dependency units). The script should: generate a properly formatted .service file, optionally install it to /etc/systemd/system/ with --install flag, reload systemd daemon, and enable/start the service. Include --dry-run to just print the unit file without installing." \
    "true" "stress_systemd_gen.sh" "bash" 15


# ============================================================================
# PHASE 4: ADVANCED FEATURES (~60 min)
# ============================================================================
log_header "PHASE 4: ADVANCED FEATURES"

run_test "BUILD" "Python" "Hard" \
    "BeanLab Monitor: WebSocket live updates" \
    "Write a Python module called websocket_handler.py that adds real-time updates to the BeanLab Monitor using Flask-SocketIO. Requirements:

1. Initialize SocketIO with the Flask app
2. Events: 'connect', 'disconnect', 'request_scan', 'subscribe_host'
3. Emit events: 'scan_complete' (broadcast), 'host_update' (to specific host subscribers), 'alert_new', 'metrics_update'
4. A function emit_scan_results(results) that the scheduler can call after each scan
5. Namespace /monitor for all dashboard events
6. Include the client-side JavaScript as a string constant that can be injected into templates
7. Track connected clients count

Add flask-socketio and python-socketio to the requirements." \
    "true" "websocket_handler.py" "python" 20

run_test "BUILD" "Python" "Hard" \
    "BeanLab Monitor: Metrics history chart data endpoint" \
    "Write a Flask route in a file called routes_charts.py that provides chart-ready data for the dashboard. Endpoints:

GET /api/charts/cpu/<host_id>?hours=24 - Returns {labels: [...timestamps], data: [...cpu_values]} for Chart.js
GET /api/charts/ram/<host_id>?hours=24 - Same for RAM
GET /api/charts/disk/<host_id>?hours=24 - Same for disk
GET /api/charts/network/<host_id>?hours=24 - Returns {labels: [...], in: [...], out: [...]} for dual-line chart
GET /api/charts/overview?hours=24 - Returns aggregated avg CPU, RAM, disk across all hosts over time
GET /api/charts/host_status_history?days=7 - Returns stacked data: how many hosts were online vs offline per hour

Each endpoint should query MetricSnapshot, aggregate appropriately (5-min granularity for <=6h, 15-min for <=24h, 1h for >24h), and return Chart.js-compatible JSON. Handle empty data gracefully." \
    "true" "routes_charts.py" "python" 20

run_test "BUILD" "JavaScript" "Medium" \
    "BeanLab Monitor: Frontend chart JavaScript" \
    "Write a JavaScript file called static/charts.js for the BeanLab Monitor dashboard that renders metrics charts using Chart.js (loaded from CDN). Functions:

1. initDashboardCharts() - Sets up the overview charts on the main dashboard
2. initHostCharts(hostId) - Sets up per-host detail charts
3. updateCharts() - Fetches latest data from /api/charts/* endpoints and updates
4. createLineChart(canvasId, label, color, data) - Helper to create a standard line chart
5. createStackedBar(canvasId, datasets) - Helper for the host status history chart
6. Auto-refresh chart data every 30 seconds

Use the bean color palette: #3E2723, #5D4037, #795548, #4CAF50, #FF9800, #F44336.
Charts should be responsive and have tooltips showing exact values." \
    "true" "static/charts.js" "javascript" 20


# ============================================================================
# STRESS INTERLUDE 4: Data & algorithms (~45 min)
# ============================================================================
log_header "STRESS INTERLUDE 4: DATA & ALGORITHMS"

run_test "STRESS" "Python" "Hard" \
    "LRU cache with TTL" \
    "Write a Python LRU cache implementation from scratch (no functools) that: supports a max_size parameter, evicts least recently used entries when full, supports an optional TTL (time-to-live) per entry in seconds, is thread-safe using threading.Lock, supports get(key), put(key, value, ttl=None), delete(key), clear(), and stats() (hits, misses, evictions, current_size), uses an OrderedDict internally, and includes comprehensive __repr__ showing cache state. Write unit tests using unittest that verify: basic get/put, eviction order, TTL expiry, thread safety with concurrent access, and stats accuracy." \
    "true" "stress_lru_cache.py" "python" 15

run_test "STRESS" "Python" "Extreme" \
    "Merkle tree implementation" \
    "Write a Python Merkle tree implementation for verifying data integrity. Include: a MerkleTree class that builds a tree from a list of data blocks, uses SHA-256 for hashing, supports get_root_hash(), get_proof(index) returning the authentication path, a static verify_proof(data, proof, root_hash) method, supports adding new blocks and rebuilding efficiently, visualization method that prints the tree structure, and serialization to/from JSON. Include a demo that builds a tree from file chunks and verifies individual chunks." \
    "true" "stress_merkle_tree.py" "python" 15

run_test "STRESS" "Python" "Hard" \
    "Task dependency resolver (topological sort)" \
    "Write a Python module that resolves task dependencies using topological sorting. Include: a TaskGraph class with add_task(name, deps=[]) method, resolve() that returns execution order using Kahn's algorithm, cycle detection that raises a clear error naming the cycle, support for task groups (multiple tasks at the same level can run in parallel), a visual ASCII graph output, and methods to find critical path and independent subgraphs. Demo with a CI/CD pipeline: lint -> test -> build -> [deploy-staging, deploy-docs] -> integration-test -> deploy-prod." \
    "true" "stress_task_resolver.py" "python" 15

run_test "STRESS" "Data" "Hard" \
    "JSON diff engine" \
    "Write a Python module that computes and displays the diff between two JSON objects. Support: nested object comparison with path tracking, array diffing (handle additions, deletions, reordering), output as a list of Change objects (path, old_value, new_value, change_type), a format_diff() method that produces a colored terminal output (green for additions, red for deletions, yellow for modifications), patch generation (list of JSON Patch RFC 6902 operations), and apply_patch() to apply a patch to a JSON object. Include type hints and dataclasses." \
    "true" "stress_json_diff.py" "python" 15


# ============================================================================
# PHASE 5: TESTING & POLISH (~45 min)
# ============================================================================
log_header "PHASE 5: TESTING & POLISH"

run_test "BUILD" "Python" "Hard" \
    "BeanLab Monitor: Test suite" \
    "Write a pytest test suite in a file called test_app.py for the BeanLab Monitor Flask application. Test:

1. App factory creates app correctly
2. Database models create and query properly
3. API endpoints: test GET /api/hosts returns JSON list, POST /api/hosts creates a host, GET /api/hosts/<id> returns details, PUT updates, DELETE removes
4. Scanner: mock ping_host and check_port, verify scan_host returns expected structure
5. Dashboard routes return 200 status codes
6. Alert creation when thresholds exceeded
7. Metrics summary aggregation is correct

Use pytest fixtures for: app (with test config), client (Flask test client), db (fresh database per test), sample_host (a pre-created host). Use unittest.mock for mocking the scanner's network calls. At least 15 test functions." \
    "true" "test_app.py" "python" 20

run_test "BUILD" "Python" "Medium" \
    "BeanLab Monitor: README.md" \
    "Write a comprehensive README.md for the BeanLab Monitor project. Include:

1. Project title with a bean emoji and tagline
2. Features list
3. Architecture overview (Flask + SQLite + background scanner)
4. Screenshots section (placeholder descriptions)
5. Quick start: git clone, python setup, run
6. Configuration: environment variables, host list format
7. API documentation: list all endpoints with method, path, description, example request/response
8. Development: how to run tests, add new hosts, modify scan interval
9. Docker deployment section
10. The BeanLab host list (all 13 servers)
11. Tech stack section
12. Credits: 'Built by Odin (Gemma 4 26B) and Loki (Qwen 2.5 Coder 14B) via n8n orchestration'
13. License: MIT" \
    "true" "README.md" "bash" 20

run_test "BUILD" "Python" "Medium" \
    "BeanLab Monitor: Makefile for common tasks" \
    "Write a Makefile for the BeanLab Monitor project with targets:
- setup: create venv, install deps
- dev: run Flask dev server
- prod: run with gunicorn
- test: run pytest with coverage
- lint: run flake8 and mypy
- seed: run seed_data.py
- clean: remove __pycache__, .pyc, instance/
- docker-build: build Docker image
- docker-run: run Docker container
- docker-compose-up: bring up full stack
- scan: trigger a manual scan via API
- db-reset: delete and recreate database
Include .PHONY declarations and helpful comments." \
    "true" "Makefile" "bash" 20


# ============================================================================
# STRESS INTERLUDE 5: Debugging & refactoring (~30 min)
# ============================================================================
log_header "STRESS INTERLUDE 5: DEBUGGING & REFACTORING"

run_test "STRESS" "Debug" "Hard" \
    "Fix async race condition" \
    "Find and fix the race condition in this async Python code:

import asyncio

counter = 0

async def increment():
    global counter
    for _ in range(1000):
        current = counter
        await asyncio.sleep(0)
        counter = current + 1

async def main():
    tasks = [increment() for _ in range(10)]
    await asyncio.gather(*tasks)
    print(f'Expected: 10000, Got: {counter}')

asyncio.run(main())

Explain the race condition, provide the fixed version using asyncio.Lock, and explain why this is different from threading race conditions." \
    "true" "stress_race_condition_fix.py" "python" 15

run_test "STRESS" "Debug" "Extreme" \
    "Refactor callback hell to async/await" \
    "Refactor this nested callback-style Python code into clean async/await:

def fetch_user(user_id, callback):
    # simulates DB call
    import time; time.sleep(0.1)
    callback({'id': user_id, 'name': 'Alice', 'team_id': 42})

def fetch_team(team_id, callback):
    import time; time.sleep(0.1)
    callback({'id': team_id, 'name': 'Engineering', 'org_id': 7})

def fetch_org(org_id, callback):
    import time; time.sleep(0.1)
    callback({'id': org_id, 'name': 'Acme Corp', 'plan': 'enterprise'})

def get_user_org(user_id, final_callback):
    def on_user(user):
        def on_team(team):
            def on_org(org):
                final_callback({
                    'user': user['name'],
                    'team': team['name'],
                    'org': org['name'],
                    'plan': org['plan']
                })
            fetch_org(team['org_id'], on_org)
        fetch_team(user['team_id'], on_team)
    fetch_user(user_id, on_user)

Provide the clean async version using asyncio, with proper error handling, type hints, and a dataclass for the result. Also write a version using asyncio.gather for parallel fetching where possible." \
    "true" "stress_callback_refactor.py" "python" 15

run_test "STRESS" "Python" "Hard" \
    "Memory-efficient large file processor" \
    "Write a Python module that processes very large files (multi-GB) memory-efficiently. Include: a chunked_reader(filepath, chunk_size=8192) generator, a line_reader(filepath) that handles partial lines at chunk boundaries, a parallel_process(filepath, worker_fn, num_workers=4) that splits the file into sections and processes each with a separate process, a progress tracker that estimates completion based on file size and current position, and support for both text and binary modes. Include a demo that counts word frequencies in a large file using the parallel processor." \
    "true" "stress_large_file.py" "python" 15


# ============================================================================
# PHASE 6: INTEGRATION & DEPLOYMENT (~45 min)
# ============================================================================
log_header "PHASE 6: INTEGRATION & DEPLOYMENT"

run_test "BUILD" "Docker" "Medium" \
    "BeanLab Monitor: Production Dockerfile" \
    "Write a production-ready Dockerfile for the BeanLab Monitor Flask app. Requirements:
- Multi-stage build with python:3.12-slim
- Stage 1 (builder): install all deps from requirements.txt
- Stage 2 (runtime): copy deps and app code, create non-root 'beanlab' user, expose port 5555
- Healthcheck using python urllib (no curl needed in slim image)
- Labels for version, description, maintainer
- CMD runs gunicorn with 2 workers on 0.0.0.0:5555
- .dockerignore as comments at the top (exclude: .git, __pycache__, *.pyc, venv, .env, instance/, tests/)
- Total image should be as small as possible" \
    "true" "Dockerfile" "dockerfile" 20

run_test "BUILD" "Docker" "Medium" \
    "BeanLab Monitor: Docker Compose deployment" \
    "Write a docker-compose.yml for deploying the BeanLab Monitor. Services:
- beanlab-monitor: build from local Dockerfile, port 5555:5555, volume for instance/ (SQLite), environment vars for SECRET_KEY and SCAN_INTERVAL=60, restart unless-stopped, network beanlab-net
- Add a comment block at the top explaining how to deploy: docker compose up -d --build

Keep it simple - just the Flask app for now. No Redis or extras." \
    "true" "docker-compose.yml" "yaml" 20

run_test "BUILD" "Bash" "Medium" \
    "BeanLab Monitor: Systemd service file" \
    "Write a systemd service unit file called beanlab-monitor.service for running the BeanLab Monitor on ai-stack-420. Configuration:
- Description: BeanLab Monitor Dashboard
- After: network-online.target docker.service
- User: cfiaschetti
- WorkingDirectory: /opt/beanlab-monitor
- ExecStart: /opt/beanlab-monitor/venv/bin/gunicorn -w 2 -b 0.0.0.0:5555 wsgi:app
- Environment: DATABASE_URL=sqlite:///instance/beanlab.db
- Restart: on-failure with 5s delay
- StandardOutput and StandardError to journal
- WantedBy: multi-user.target

Also write a brief install.sh script that copies the service file, reloads systemd, enables and starts the service." \
    "true" "beanlab-monitor.service" "bash" 20


# ============================================================================
# STRESS INTERLUDE 6: Advanced patterns (~45 min)
# ============================================================================
log_header "STRESS INTERLUDE 6: ADVANCED PATTERNS"

run_test "STRESS" "Python" "Extreme" \
    "Circuit breaker pattern" \
    "Write a Python implementation of the circuit breaker pattern for resilient service calls. Include: a CircuitBreaker class with states (CLOSED, OPEN, HALF_OPEN), configurable failure_threshold, recovery_timeout, and success_threshold, a @circuit_breaker decorator for wrapping function calls, automatic state transitions based on failure/success counts, event callbacks for state changes, metrics (total calls, failures, state changes, last failure time), thread-safe implementation, and a demo simulating a flaky API with random failures showing the breaker in action." \
    "true" "stress_circuit_breaker.py" "python" 15

run_test "STRESS" "Python" "Extreme" \
    "Observable data store (reactive pattern)" \
    "Write a Python reactive data store that: implements an Observable class supporting subscribe(callback), unsubscribe(), and notify(), a Store class with get(key), set(key, value), delete(key), a computed() decorator that auto-recomputes when dependencies change, middleware support for logging, validation, and undo/redo, transaction support (batch multiple changes, commit/rollback), and JSON serialization of store state. Demo: a user store where computed 'full_name' updates when 'first_name' or 'last_name' changes, with undo support." \
    "true" "stress_reactive_store.py" "python" 15

run_test "STRESS" "Python" "Hard" \
    "Custom test framework" \
    "Write a minimal Python test framework from scratch (not using unittest or pytest). Features: @test decorator to mark test functions, assert_equal, assert_raises, assert_true helper functions, test discovery from modules, setup/teardown support per test and per module, colored terminal output with pass/fail/error counts, timing per test, a --filter flag to run only matching tests, and XML output compatible with JUnit format. Demo with at least 10 tests covering a simple Calculator class." \
    "true" "stress_test_framework.py" "python" 15

run_test "STRESS" "Python" "Hard" \
    "HTTP request retry middleware" \
    "Write a Python requests middleware/adapter that adds: configurable retry logic with exponential backoff and jitter, per-status-code retry rules (retry on 429, 500, 502, 503, 504), circuit breaker integration (stop retrying if service is consistently down), request/response logging with configurable verbosity, rate limiting (max N requests per second), request deduplication (don't send identical requests within N seconds), and metrics collection (latency histogram, status code counts). Use requests.adapters.HTTPAdapter as the base. Include a demo." \
    "true" "stress_http_middleware.py" "python" 15


# ============================================================================
# STRESS INTERLUDE 7: Routing & edge cases (~30 min)
# ============================================================================
log_header "STRESS INTERLUDE 7: ROUTING & EDGE CASES"

run_test "STRESS" "Routing" "Easy" \
    "Pure architecture question" \
    "What are the key differences between horizontal and vertical scaling? When would you choose each for a homelab environment with limited hardware?" \
    "false" "" "python" 10

run_test "STRESS" "Routing" "Medium" \
    "Conceptual + code boundary" \
    "Explain the CAP theorem and demonstrate it with a simple Python simulation showing how a distributed key-value store behaves when a network partition occurs. The simulation should show the tradeoff between consistency and availability." \
    "true" "stress_cap_demo.py" "python" 15

run_test "STRESS" "Routing" "Hard" \
    "Deeply ambiguous request" \
    "I need to understand how my homelab handles DNS. Walk me through what happens when a device on my network tries to resolve a domain name, considering I have Pi-hole at 192.168.1.228 as my DNS server. Include any code or configuration that would help me troubleshoot DNS issues." \
    "either" "" "python" 10

run_test "STRESS" "Routing" "Easy" \
    "Opinion question (should be direct)" \
    "Is it better to run AI models locally or use cloud APIs? Consider cost, latency, privacy, and capability tradeoffs for a homelab enthusiast." \
    "false" "" "python" 10


# ============================================================================
# STRESS INTERLUDE 8: Final gauntlet (~60 min)
# ============================================================================
log_header "STRESS INTERLUDE 8: FINAL GAUNTLET"

run_test "STRESS" "Python" "Extreme" \
    "Mini ORM from scratch" \
    "Write a minimal Python ORM that maps classes to SQLite tables. Support: a Model base class with save(), delete(), and classmethods all(), filter(**kwargs), get(id), automatic table creation from class attributes (IntField, StringField, FloatField, BoolField, DateTimeField, ForeignKeyField), basic query building with chaining (.filter().order_by().limit()), migration support (detect schema changes and ALTER TABLE), connection pooling with context managers, and a relationship() descriptor for lazy-loading related objects. Demo with User and Post models." \
    "true" "stress_mini_orm.py" "python" 20

run_test "STRESS" "Python" "Extreme" \
    "Distributed task queue (simplified)" \
    "Write a simplified distributed task queue in Python using only stdlib (threading, queue, socket, json, pickle). Include: a TaskBroker that listens on a TCP port and distributes tasks, a Worker class that connects to the broker and processes tasks, a Client class that submits tasks and retrieves results, task serialization/deserialization, task status tracking (pending, running, completed, failed), result storage with TTL, retry logic for failed tasks (max 3 attempts), and graceful shutdown for all components. Demo with a broker, 3 workers, and a client submitting 20 math tasks." \
    "true" "stress_task_queue.py" "python" 20

run_test "STRESS" "Python" "Extreme" \
    "Expression parser and evaluator" \
    "Write a Python mathematical expression parser and evaluator from scratch. Support: basic arithmetic (+, -, *, /, **, %), parentheses for grouping, variable assignment and lookup (x = 5; x + 3), built-in functions (sin, cos, sqrt, abs, min, max, log), proper operator precedence and associativity, a tokenizer (lexer), recursive descent parser building an AST, an AST evaluator, clear error messages for syntax errors with position indicators, and a REPL mode. No eval() or ast.literal_eval — build the parser from scratch." \
    "true" "stress_expr_parser.py" "python" 20

run_test "STRESS" "Python" "Extreme" \
    "Git-like version control (simplified)" \
    "Write a simplified version control system in Python inspired by Git. Implement: init() creating a .minivc directory structure, add(filepath) staging files by computing their SHA-256 hash, commit(message) creating a commit object with tree hash, parent, author, timestamp, log() showing commit history, diff(commit1, commit2) showing changed files, checkout(commit_hash) restoring files to a specific commit state, branch(name) and switch(name) for basic branching, status() showing modified/staged/untracked files. Store objects as JSON files hashed by content. Demo with a sequence of file changes, commits, and a branch." \
    "true" "stress_mini_vcs.py" "python" 20

run_test "STRESS" "Bash" "Extreme" \
    "Full infrastructure audit script" \
    "Write a comprehensive bash script that performs an infrastructure audit on a Linux server. Check and report on: OS version and kernel, CPU info and load averages, memory usage and swap, disk usage per mount and inode usage, network interfaces and IP addresses, open ports (ss -tlnp), running services (systemctl), Docker containers and their status, firewall rules (iptables/nftables), DNS resolution test, NTP sync status, failed systemd units, users with login shells, SSH config security (PermitRootLogin, PasswordAuth), last 10 failed login attempts, uptime and reboot history. Output as both a formatted terminal report and a JSON file. Support a --quick flag that skips slow checks." \
    "true" "stress_infra_audit.sh" "bash" 20

run_test "STRESS" "SQL" "Hard" \
    "Complex SQL query generator" \
    "Write a Python module that generates complex SQL queries from a high-level description language. Support: a fluent API for building queries (Query.select('users.name', 'orders.total').from_('users').join('orders', on='users.id = orders.user_id').where('orders.total > ?', 100).group_by('users.name').having('COUNT(*) > ?', 5).order_by('total DESC').limit(10)), subquery support (as both table source and WHERE IN clause), Common Table Expression (CTE/WITH) support, UNION/INTERSECT/EXCEPT, window functions (OVER, PARTITION BY, ORDER BY), pretty-print with proper indentation, and parameter tracking for safe execution. All values parameterized. Include 5 complex query examples demonstrating all features." \
    "true" "stress_sql_builder.py" "python" 20


# ============================================================================
# FINAL HEALTH CHECK
# ============================================================================
log_header "POST-TEST HEALTH CHECK"

log "Running final health check..."
if health_check; then
    log "Pipeline still healthy after full test run"
else
    log "WARNING: Pipeline may be degraded after extended run"
fi


# ============================================================================
# RESULTS SUMMARY
# ============================================================================
SUITE_END=$(date +%s)
SUITE_START_EPOCH=$(date -d "$TIMESTAMP" +%s 2>/dev/null || echo $((SUITE_END - TOTAL_TIME_SEC)))
SUITE_ELAPSED=$((SUITE_END - SUITE_START_EPOCH))
SUITE_HOURS=$((SUITE_ELAPSED / 3600))
SUITE_MIN=$(( (SUITE_ELAPSED % 3600) / 60 ))

log_header "OVERNIGHT ENDURANCE TEST COMPLETE"

BUILD_COUNT=$(grep -c "^[0-9]*,BUILD," "$CSV_LOG" 2>/dev/null || echo 0)
STRESS_COUNT=$(grep -c "^[0-9]*,STRESS," "$CSV_LOG" 2>/dev/null || echo 0)
SYNTAX_CHECKED=$((SYNTAX_PASS + SYNTAX_FAIL))

cat << SUMMARY | tee -a "$MASTER_LOG"

╔══════════════════════════════════════════════════════════════╗
║              OVERNIGHT ENDURANCE TEST RESULTS               ║
╚══════════════════════════════════════════════════════════════╝

  Total tests:        ${TOTAL}
    Build steps:      ${BUILD_COUNT}
    Stress tests:     ${STRESS_COUNT}

  Pass rate:          $( [[ $TOTAL -gt 0 ]] && echo "$((PASSED * 100 / TOTAL))%" || echo "N/A")
    Passed:           ${PASSED}
    Failed:           ${FAILED}
    Errors:           ${ERRORS}

  Syntax checks:      ${SYNTAX_CHECKED}
    Syntax pass:      ${SYNTAX_PASS}
    Syntax fail:      ${SYNTAX_FAIL}

  Total duration:     ${SUITE_HOURS}h ${SUITE_MIN}m
  Avg per test:       $( [[ $TOTAL -gt 0 ]] && echo "$((TOTAL_TIME_SEC / TOTAL))s" || echo "N/A")

  Output directory:   ${OUTPUT_DIR}
  Project code:       ${PROJECT_DIR}
  Results CSV:        ${CSV_LOG}
  Master log:         ${MASTER_LOG}
  Syntax log:         ${SYNTAX_LOG}

  Project files built:
$(ls -la "$PROJECT_DIR"/ 2>/dev/null | tail -n +2 || echo "    (none)")

SUMMARY

# Category breakdown
echo "" | tee -a "$MASTER_LOG"
echo "Category Breakdown:" | tee -a "$MASTER_LOG"
echo "─────────────────────────────────────────────────" | tee -a "$MASTER_LOG"
awk -F',' 'NR>1 {
    cat=$3
    total[cat]++
    if ($6=="PASS") pass[cat]++
    time[cat]+=$7
    retries[cat]+=$8
    if ($11=="PASS") syn_pass[cat]++
    if ($11=="FAIL") syn_fail[cat]++
} END {
    for (c in total) {
        p = (c in pass) ? pass[c] : 0
        sp = (c in syn_pass) ? syn_pass[c] : 0
        sf = (c in syn_fail) ? syn_fail[c] : 0
        printf "  %-14s %d/%d passed  avg %ds  retries: %d  syntax: %d/%d\n", c, p, total[c], time[c]/total[c], retries[c], sp, sp+sf
    }
}' "$CSV_LOG" | tee -a "$MASTER_LOG"

echo "" | tee -a "$MASTER_LOG"
echo "─────────────────────────────────────────────────" | tee -a "$MASTER_LOG"
echo "Completed: $(date)" | tee -a "$MASTER_LOG"
echo "" | tee -a "$MASTER_LOG"
echo "To review the BeanLab Monitor project:" | tee -a "$MASTER_LOG"
echo "  cd ${PROJECT_DIR}" | tee -a "$MASTER_LOG"
echo "  ls -la" | tee -a "$MASTER_LOG"
echo "" | tee -a "$MASTER_LOG"
