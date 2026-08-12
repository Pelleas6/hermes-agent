#!/usr/bin/env bash
set -euo pipefail

SOURCE_REPO="${SOURCE_REPO:-Pelleas6/hermes-agent}"
SOURCE_REF="${SOURCE_REF:-main}"
ROOT="${HERMES_SHARED_ROOT:-/opt/data}"
HERMES="${HERMES_BIN:-/opt/hermes/.venv/bin/hermes}"
PY="${HERMES_PYTHON:-/opt/hermes/.venv/bin/python3}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
STAGE="/tmp/hermes-observability-${STAMP}"
BACKUP="$ROOT/backups/observability-install-${STAMP}"

if [[ ! -x "$HERMES" ]]; then
  echo "ERREUR: binaire Hermes absent: $HERMES" >&2
  exit 1
fi
if [[ ! -x "$PY" ]]; then
  echo "ERREUR: Python Hermes absent: $PY" >&2
  exit 1
fi

mkdir -p "$STAGE" "$BACKUP" "$ROOT/metrics"

cleanup() {
  rm -rf "$STAGE"
}
trap cleanup EXIT

raw_url() {
  printf 'https://raw.githubusercontent.com/%s/%s/%s' "$SOURCE_REPO" "$SOURCE_REF" "$1"
}

download() {
  local rel="$1"
  local dst="$STAGE/$rel"
  mkdir -p "$(dirname "$dst")"
  "$PY" - "$dst" "$(raw_url "$rel")" <<'PY'
import pathlib, sys, urllib.request
path = pathlib.Path(sys.argv[1])
url = sys.argv[2]
request = urllib.request.Request(url, headers={"User-Agent": "Hermes-Observability-Installer/1.0"})
with urllib.request.urlopen(request, timeout=45) as response:
    data = response.read()
if not data:
    raise SystemExit(f"empty download: {url}")
path.write_bytes(data)
print(f"downloaded {path.name}: {len(data)} bytes")
PY
}

FILES=(
  "plugins/task-metrics/plugin.yaml"
  "plugins/task-metrics/__init__.py"
  "plugins/task-metrics/storage.py"
  "plugins/task-metrics/README.md"
  "skills/productivity/task-time-metrics/SKILL.md"
  "skills/productivity/task-time-metrics/scripts/task_time_tracker.py"
  "ops/observability/ops_observability.py"
  "ops/observability/ops_snapshot_cron.py"
  "ops/observability/ops_daily_alert_cron.py"
  "ops/observability/ops_weekly_report_cron.py"
  "ops/observability/hermes_observability/__init__.py"
  "ops/observability/hermes_observability/common.py"
  "ops/observability/hermes_observability/system_tasks.py"
  "ops/observability/hermes_observability/cron_kanban.py"
  "ops/observability/hermes_observability/integrations.py"
  "ops/observability/hermes_observability/supabase.py"
  "ops/observability/hermes_observability/health_render.py"
  "ops/observability/hermes_observability/persistence.py"
  "ops/observability/hermes_observability/app.py"
  "ops/observability/observability_config.example.json"
)

echo "=== Téléchargement vérifiable (${SOURCE_REPO}@${SOURCE_REF}) ==="
for rel in "${FILES[@]}"; do
  download "$rel"
done

"$PY" -m compileall -q \
  "$STAGE/plugins/task-metrics" \
  "$STAGE/skills/productivity/task-time-metrics/scripts" \
  "$STAGE/ops/observability"

echo "=== Inventaire des profils réels ==="
PROFILE_NAMES=("default")
PROFILE_HOMES=("$ROOT")
if [[ -d "$ROOT/profiles" ]]; then
  while IFS= read -r -d '' profile_dir; do
    PROFILE_NAMES+=("$(basename "$profile_dir")")
    PROFILE_HOMES+=("$profile_dir")
  done < <(find "$ROOT/profiles" -mindepth 1 -maxdepth 1 -type d -print0 | sort -z)
fi
printf 'Profils: %s\n' "${PROFILE_NAMES[*]}"

STATE_FILE="$BACKUP/install-state.tsv"
printf 'kind\tname\thome\ttarget\texisted\n' > "$STATE_FILE"


install_profile() {
  local name="$1"
  local home="$2"
  local label="profile-$name"
  local plugin_dest="$home/plugins/task-metrics"
  local skill_dest="$home/skills/productivity/task-time-metrics"
  local plugin_existed=0 skill_existed=0 config_existed=0
  [[ -d "$plugin_dest" ]] && plugin_existed=1
  [[ -d "$skill_dest" ]] && skill_existed=1
  [[ -f "$home/config.yaml" ]] && config_existed=1
  printf 'profile\t%s\t%s\tplugin\t%s\n' "$name" "$home" "$plugin_existed" >> "$STATE_FILE"
  printf 'profile\t%s\t%s\tskill\t%s\n' "$name" "$home" "$skill_existed" >> "$STATE_FILE"
  printf 'profile\t%s\t%s\tconfig\t%s\n' "$name" "$home" "$config_existed" >> "$STATE_FILE"

  echo "--- Installation profil $name ($home) ---"
  if [[ -d "$plugin_dest" ]]; then
    mkdir -p "$BACKUP/$label/plugins"
    cp -a "$plugin_dest" "$BACKUP/$label/plugins/task-metrics"
  fi
  if [[ -d "$skill_dest" ]]; then
    mkdir -p "$BACKUP/$label/skills/productivity"
    cp -a "$skill_dest" "$BACKUP/$label/skills/productivity/task-time-metrics"
  fi
  if [[ -f "$home/config.yaml" ]]; then
    mkdir -p "$BACKUP/$label"
    cp -a "$home/config.yaml" "$BACKUP/$label/config.yaml"
  fi

  mkdir -p "$plugin_dest" "$skill_dest/scripts"
  install -m 0644 "$STAGE/plugins/task-metrics/plugin.yaml" "$plugin_dest/plugin.yaml"
  install -m 0644 "$STAGE/plugins/task-metrics/__init__.py" "$plugin_dest/__init__.py"
  install -m 0644 "$STAGE/plugins/task-metrics/storage.py" "$plugin_dest/storage.py"
  install -m 0644 "$STAGE/plugins/task-metrics/README.md" "$plugin_dest/README.md"
  install -m 0644 "$STAGE/skills/productivity/task-time-metrics/SKILL.md" "$skill_dest/SKILL.md"
  install -m 0755 "$STAGE/skills/productivity/task-time-metrics/scripts/task_time_tracker.py" "$skill_dest/scripts/task_time_tracker.py"

  if ! HERMES_HOME="$home" "$HERMES" plugins enable task-metrics >"/tmp/task-metrics-enable-${name}.log" 2>&1; then
    echo "INFO: activation CLI indisponible pour $name, fallback config.yaml"
    enable_plugin_in_yaml "$home/config.yaml"
  fi
}

enable_plugin_in_yaml() {
  local config="$1"
  "$PY" - "$config" <<'PY'
import pathlib, sys
import yaml
path = pathlib.Path(sys.argv[1])
data = {}
if path.exists():
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if isinstance(loaded, dict):
        data = loaded
plugins = data.setdefault("plugins", {})
if not isinstance(plugins, dict):
    plugins = {}
    data["plugins"] = plugins
enabled = plugins.get("enabled")
if not isinstance(enabled, list):
    enabled = []
if "task-metrics" not in enabled:
    enabled.append("task-metrics")
plugins["enabled"] = enabled
disabled = plugins.get("disabled")
if isinstance(disabled, list):
    plugins["disabled"] = [item for item in disabled if item != "task-metrics"]
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
PY
}

record_root_file() {
  local rel="$1"
  local src="$ROOT/$rel"
  local existed=0
  [[ -e "$src" ]] && existed=1
  printf 'rootfile\tdefault\t%s\t%s\t%s\n' "$ROOT" "$rel" "$existed" >> "$STATE_FILE"
  if [[ "$existed" == "1" ]]; then
    mkdir -p "$BACKUP/root-files/$(dirname "$rel")"
    cp -a "$src" "$BACKUP/root-files/$rel"
  fi
}

# Bash resolves functions at execution time, so the helpers above are
# available when install_profile invokes them.
for index in "${!PROFILE_NAMES[@]}"; do
  install_profile "${PROFILE_NAMES[$index]}" "${PROFILE_HOMES[$index]}"
done

echo "=== Installation des scripts d’observabilité ==="
mkdir -p "$ROOT/scripts"
for script in ops_observability.py ops_snapshot_cron.py ops_daily_alert_cron.py ops_weekly_report_cron.py; do
  existed=0
  [[ -f "$ROOT/scripts/$script" ]] && existed=1
  printf 'script\tdefault\t%s\t%s\t%s\n' "$ROOT/scripts" "$script" "$existed" >> "$STATE_FILE"
  if [[ "$existed" == "1" ]]; then
    mkdir -p "$BACKUP/default-scripts"
    cp -a "$ROOT/scripts/$script" "$BACKUP/default-scripts/$script"
  fi
  install -m 0755 "$STAGE/ops/observability/$script" "$ROOT/scripts/$script"
done

PACKAGE_DEST="$ROOT/scripts/hermes_observability"
package_existed=0
[[ -d "$PACKAGE_DEST" ]] && package_existed=1
printf 'script\tdefault\t%s\thermes_observability\t%s\n' "$ROOT/scripts" "$package_existed" >> "$STATE_FILE"
if [[ "$package_existed" == "1" ]]; then
  mkdir -p "$BACKUP/default-scripts"
  cp -a "$PACKAGE_DEST" "$BACKUP/default-scripts/hermes_observability"
fi
rm -rf "$PACKAGE_DEST"
mkdir -p "$PACKAGE_DEST"
for module in __init__.py common.py system_tasks.py cron_kanban.py integrations.py supabase.py health_render.py persistence.py app.py; do
  install -m 0644 "$STAGE/ops/observability/hermes_observability/$module" "$PACKAGE_DEST/$module"
done

CONFIG="$ROOT/metrics/observability_config.json"
record_root_file "metrics/observability_config.json"
if [[ ! -f "$CONFIG" ]]; then
  install -m 0644 "$STAGE/ops/observability/observability_config.example.json" "$CONFIG"
fi

export HERMES_SHARED_ROOT="$ROOT"
export HERMES_TASK_TIME_DB="$ROOT/metrics/task_time.sqlite3"

TRACKER="$ROOT/skills/productivity/task-time-metrics/scripts/task_time_tracker.py"
"$PY" "$TRACKER" --db "$HERMES_TASK_TIME_DB" doctor --stale-after-hours 24 >/tmp/task-time-doctor.json

# Plugin smoke test against an isolated database: imports the actual installed
# plugin, registers hooks, records one synthetic turn, and verifies persistence.
"$PY" - "$ROOT/plugins/task-metrics" "$ROOT/metrics/.task-metrics-smoke.sqlite3" <<'PY'
import importlib.util, os, pathlib, sqlite3, sys
plugin_dir = pathlib.Path(sys.argv[1])
db = pathlib.Path(sys.argv[2])
for suffix in ("", "-wal", "-shm"):
    pathlib.Path(str(db) + suffix).unlink(missing_ok=True)
os.environ["HERMES_TASK_TIME_DB"] = str(db)
os.environ["HERMES_HOME"] = str(plugin_dir.parents[1])
spec = importlib.util.spec_from_file_location(
    "task_metrics_vps_smoke", plugin_dir / "__init__.py",
    submodule_search_locations=[str(plugin_dir)],
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
class Context:
    def __init__(self): self.hooks = {}; self.commands = {}
    def register_hook(self, name, callback): self.hooks[name] = callback
    def register_command(self, name, handler, description=""): self.commands[name] = handler
ctx = Context(); module.register(ctx)
required = {"pre_api_request", "post_api_request", "post_tool_call", "on_session_end", "kanban_task_claimed", "kanban_task_completed", "kanban_task_blocked"}
if not required.issubset(ctx.hooks):
    raise SystemExit(f"missing hooks: {sorted(required - set(ctx.hooks))}")
ctx.hooks["pre_api_request"](session_id="smoke", task_id="smoke-task", model="smoke", provider="smoke")
ctx.hooks["post_api_request"](session_id="smoke", task_id="smoke-task", model="smoke", provider="smoke", api_duration=0.001, usage={"input_tokens": 1})
ctx.hooks["on_session_end"](session_id="smoke", task_id="smoke-task", completed=True)
conn = sqlite3.connect(db)
row = conn.execute("SELECT status,outcome FROM tasks LIMIT 1").fetchone()
obs = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
conn.close()
if row != ("completed", "success") or obs < 2:
    raise SystemExit(f"smoke persistence failed: row={row} observations={obs}")
print(f"plugin smoke OK: hooks={len(ctx.hooks)} observations={obs}")
PY
rm -f "$ROOT/metrics/.task-metrics-smoke.sqlite3"{,-wal,-shm}

# Snapshot + dashboard + verified backup. Rebuild the snapshot after the
# backup so the dashboard immediately contains the new restore-test evidence.
"$PY" "$ROOT/scripts/ops_observability.py" --root "$ROOT" snapshot --since 7d --quiet
"$PY" "$ROOT/scripts/ops_observability.py" --root "$ROOT" backup --retention-days 14 >/tmp/observability-backup.json
"$PY" "$ROOT/scripts/ops_observability.py" --root "$ROOT" snapshot --since 7d --quiet

record_root_file "cron/jobs.json"

cron_name_exists() {
  local name="$1"
  "$PY" - "$ROOT/cron/jobs.json" "$name" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1]); wanted = sys.argv[2]
if not path.exists(): raise SystemExit(1)
try: payload = json.loads(path.read_text(encoding="utf-8"))
except Exception: raise SystemExit(1)
if isinstance(payload, dict):
    jobs = payload.get("jobs") or payload.get("items") or payload.get("data") or []
    if not isinstance(jobs, list): jobs = list(payload.values())
elif isinstance(payload, list): jobs = payload
else: jobs = []
for job in jobs:
    if isinstance(job, dict) and str(job.get("name") or job.get("title") or "") == wanted:
        raise SystemExit(0)
raise SystemExit(1)
PY
}

choose_delivery() {
  if [[ -n "${OBSERVABILITY_DELIVER:-}" ]]; then
    printf '%s' "$OBSERVABILITY_DELIVER"
    return
  fi
  "$PY" - "$ROOT/cron/jobs.json" "$ROOT/.env" <<'PY'
import json, pathlib, re, sys
jobs_path, env_path = map(pathlib.Path, sys.argv[1:])
if jobs_path.exists():
    try: payload = json.loads(jobs_path.read_text(encoding="utf-8"))
    except Exception: payload = []
    if isinstance(payload, dict): payload = payload.get("jobs") or list(payload.values())
    if isinstance(payload, list):
        for job in payload:
            if not isinstance(job, dict): continue
            for key in ("deliver", "delivery_target", "delivery", "target"):
                value = job.get(key)
                if isinstance(value, str) and value.startswith("telegram"):
                    print(value); raise SystemExit(0)
if env_path.exists():
    text = env_path.read_text(encoding="utf-8", errors="ignore")
    if re.search(r"^TELEGRAM_BOT_TOKEN=.+", text, re.M):
        print("telegram"); raise SystemExit(0)
print("local")
PY
}

DELIVER_TARGET="$(choose_delivery)"
echo "Canal rapports observabilité: $DELIVER_TARGET"

create_job() {
  local name="$1" schedule="$2" script="$3" deliver="$4"
  if cron_name_exists "$name"; then
    echo "Cron déjà présent: $name"
    return
  fi
  HERMES_HOME="$ROOT" "$HERMES" cron create "$schedule" \
    --no-agent \
    --script "$script" \
    --deliver "$deliver" \
    --name "$name"
}

create_job "Observabilite snapshot 15 min" "every 15m" "ops_snapshot_cron.py" "local"
create_job "Observabilite alerte quotidienne" "15 8 * * *" "ops_daily_alert_cron.py" "$DELIVER_TARGET"
create_job "Observabilite rapport hebdomadaire" "30 8 * * 1" "ops_weekly_report_cron.py" "$DELIVER_TARGET"

# Restart only gateway services that are currently UP. Never start a stopped
# profile as a side effect of observability installation.
RESTARTED=()
if command -v s6-svstat >/dev/null 2>&1 && command -v s6-svc >/dev/null 2>&1; then
  for index in "${!PROFILE_NAMES[@]}"; do
    name="${PROFILE_NAMES[$index]}"
    if [[ "$name" == "default" ]]; then
      candidates=("/run/service/gateway" "/run/service/gateway-default")
    else
      candidates=("/run/service/gateway-$name")
    fi
    for service in "${candidates[@]}"; do
      if [[ -d "$service" ]] && s6-svstat "$service" 2>/dev/null | grep -q '^up'; then
        s6-svc -r "$service"
        RESTARTED+=("$name")
        break
      fi
    done
  done
fi

cat > "$BACKUP/rollback.sh" <<'ROLLBACK'
#!/usr/bin/env bash
set -euo pipefail
BACKUP_DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_FILE="$BACKUP_DIR/install-state.tsv"
if [[ ! -f "$STATE_FILE" ]]; then
  echo "ERREUR: état d'installation absent: $STATE_FILE" >&2
  exit 1
fi
while IFS=$'\t' read -r kind name home target existed; do
  [[ "$kind" == "kind" ]] && continue
  if [[ "$kind" == "profile" ]]; then
    label="profile-$name"
    case "$target" in
      plugin)
        dest="$home/plugins/task-metrics"
        src="$BACKUP_DIR/$label/plugins/task-metrics"
        ;;
      skill)
        dest="$home/skills/productivity/task-time-metrics"
        src="$BACKUP_DIR/$label/skills/productivity/task-time-metrics"
        ;;
      config)
        dest="$home/config.yaml"
        src="$BACKUP_DIR/$label/config.yaml"
        ;;
      *) continue ;;
    esac
    if [[ "$existed" == "1" ]]; then
      rm -rf "$dest"
      mkdir -p "$(dirname "$dest")"
      cp -a "$src" "$dest"
    else
      rm -rf "$dest"
    fi
  elif [[ "$kind" == "script" ]]; then
    dest="$home/$target"
    src="$BACKUP_DIR/default-scripts/$target"
    if [[ "$existed" == "1" ]]; then
      rm -rf "$dest"
      mkdir -p "$(dirname "$dest")"
      cp -a "$src" "$dest"
    else
      rm -rf "$dest"
    fi
  elif [[ "$kind" == "rootfile" ]]; then
    dest="$home/$target"
    src="$BACKUP_DIR/root-files/$target"
    if [[ "$existed" == "1" ]]; then
      rm -rf "$dest"
      mkdir -p "$(dirname "$dest")"
      cp -a "$src" "$dest"
    else
      rm -rf "$dest"
    fi
  fi
done < "$STATE_FILE"
echo "Rollback plugin/skill/scripts/config/crons terminé. Redémarrer les gateways Hermes déjà actifs pour recharger les plugins."
ROLLBACK
chmod 0700 "$BACKUP/rollback.sh"

echo "=== Vérification finale ==="
"$PY" "$ROOT/scripts/ops_observability.py" --root "$ROOT" doctor || {
  echo "ERREUR: doctor observabilité en échec" >&2
  exit 1
}

"$PY" - "$ROOT" "${PROFILE_NAMES[@]}" <<'PY'
import pathlib, sys, yaml
root = pathlib.Path(sys.argv[1]); names = sys.argv[2:]
for name in names:
    home = root if name == "default" else root / "profiles" / name
    config = home / "config.yaml"
    data = yaml.safe_load(config.read_text(encoding="utf-8")) if config.exists() else {}
    enabled = ((data or {}).get("plugins") or {}).get("enabled") or []
    if "task-metrics" not in enabled:
        raise SystemExit(f"plugin not enabled for {name}")
    for path in (
        home / "plugins" / "task-metrics" / "plugin.yaml",
        home / "plugins" / "task-metrics" / "__init__.py",
        home / "plugins" / "task-metrics" / "storage.py",
        home / "skills" / "productivity" / "task-time-metrics" / "SKILL.md",
    ):
        if not path.is_file() or path.stat().st_size == 0:
            raise SystemExit(f"missing {path}")
print(f"verified profiles: {len(names)}")
PY

"$PY" - "$ROOT/cron/jobs.json" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
if isinstance(payload, dict):
    jobs = payload.get("jobs") or payload.get("items") or payload.get("data") or []
    if not isinstance(jobs, list):
        jobs = [value for value in payload.values() if isinstance(value, dict)]
elif isinstance(payload, list):
    jobs = payload
else:
    jobs = []
names = {str(job.get("name") or job.get("title") or "") for job in jobs if isinstance(job, dict)}
required = {
    "Observabilite snapshot 15 min",
    "Observabilite alerte quotidienne",
    "Observabilite rapport hebdomadaire",
}
missing = sorted(required - names)
if missing:
    raise SystemExit(f"missing observability crons: {missing}")
print("verified no-agent cron definitions: 3")
PY

cat <<EOF

INSTALLATION OBSERVABILITÉ TERMINÉE
- Source: ${SOURCE_REPO}@${SOURCE_REF}
- Profils instrumentés: ${#PROFILE_NAMES[@]}
- Base partagée: $ROOT/metrics/task_time.sqlite3
- Snapshot: $ROOT/metrics/current.json
- Rapport: $ROOT/metrics/current.md
- Dashboard: $ROOT/metrics/dashboard.html
- Backups vérifiés + tests de restauration: $ROOT/backups/observability/
- Crons no-agent: snapshot 15 min, alerte quotidienne, rapport hebdomadaire
- Gateways redémarrés (uniquement ceux déjà actifs): ${RESTARTED[*]:-aucun}
- Sauvegarde pré-installation: $BACKUP
- Rollback: $BACKUP/rollback.sh
EOF
