#!/bin/zsh

set -u

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_PORT="${TOOLBOX_PORT:-4791}"
NPM_CACHE_DIR="${TMPDIR:-/tmp}/ai-toolbox-npm-cache-${UID}"
IDENTITY_FILE="$ROOT_DIR/registry/workbench_identity.json"

print_status() {
  echo ""
  echo "$1"
}

find_python() {
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  return 1
}

read_identity() {
  "$PYTHON_BIN" - "$ROOT_DIR" "$IDENTITY_FILE" <<'PY'
import hashlib
import json
import os
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1]).resolve(strict=True)
identity_path = pathlib.Path(sys.argv[2])
if identity_path.is_symlink():
    raise SystemExit(2)
raw = identity_path.read_bytes()
if len(raw) > 4096:
    raise SystemExit(2)
row = json.loads(raw.decode("utf-8"))
if (
    not isinstance(row, dict)
    or set(row) != {"schema_version", "app", "api_version", "build_id"}
    or row["schema_version"] != 1
    or row["app"] != "ai-toolbox-workbench"
    or row["api_version"] != "v1"
    or not isinstance(row["build_id"], str)
    or re.fullmatch(r"[a-z0-9][a-z0-9._-]{7,79}", row["build_id"]) is None
):
    raise SystemExit(2)
source_id = hashlib.sha256(os.fsencode(str(root))).hexdigest()[:16]
print("\t".join((row["app"], row["api_version"], row["build_id"], source_id)))
PY
}

health_state() {
  local port="$1"
  "$PYTHON_BIN" - "$port" "$APP_ID" "$EXPECTED_API_VERSION" "$BUILD_ID" "$SOURCE_ID" <<'PY'
import json
import sys
import urllib.error
import urllib.request

port, app_id, api_version, build_id, source_id = sys.argv[1:]
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
        payload = json.loads(response.read(65537).decode("utf-8"))
except Exception:
    print("unreachable")
    raise SystemExit(0)
if payload.get("app") != app_id:
    print("other")
elif (
    payload.get("api_version") == api_version
    and payload.get("build_id") == build_id
    and payload.get("source_id") == source_id
):
    print("exact")
elif payload.get("source_id") == source_id:
    print("same-source-old")
else:
    print("other")
PY
}

is_port_busy() {
  local port="$1"
  lsof -n -P -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
}

pid_file_for_port() {
  local port="$1"
  echo "${TMPDIR:-/tmp}/ai-toolbox-workbench-${SOURCE_ID}-${port}.pid"
}

stop_managed_old_service() {
  local port="$1"
  local pid_file pid cwd listener
  pid_file="$(pid_file_for_port "$port")"
  [[ -f "$pid_file" && ! -L "$pid_file" ]] || return 1
  pid="$(<"$pid_file")"
  [[ "$pid" == <-> ]] || return 1
  kill -0 "$pid" >/dev/null 2>&1 || return 1
  cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1)"
  listener="$(lsof -n -P -a -p "$pid" -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | head -n 1)"
  [[ "$cwd" == "$ROOT_DIR" && "$listener" == "$pid" ]] || return 1
  kill -TERM "$pid" >/dev/null 2>&1 || return 1
  for _ in {1..50}; do
    kill -0 "$pid" >/dev/null 2>&1 || break
    sleep 0.1
  done
  if kill -0 "$pid" >/dev/null 2>&1; then
    return 1
  fi
  rm -f -- "$pid_file"
  return 0
}

choose_port() {
  local first="$1"
  local port state
  local ports
  ports=("$first" 4791 4792 4793 4794 4795 4796 4797 4798 4799)

  for port in "${ports[@]}"; do
    state="$(health_state "$port")"
    if [[ "$state" == "exact" ]]; then
      echo "${port}:running"
      return 0
    fi
  done

  for port in "${ports[@]}"; do
    state="$(health_state "$port")"
    if [[ "$state" == "same-source-old" ]] && stop_managed_old_service "$port"; then
      echo "${port}:new"
      return 0
    fi
  done

  for port in "${ports[@]}"; do
    if ! is_port_busy "$port"; then
      echo "${port}:new"
      return 0
    fi
  done

  return 1
}

cd "$ROOT_DIR" || exit 1

if [[ ! -f "app.py" || ! -f "package-lock.json" || ! -f "$IDENTITY_FILE" ]]; then
  print_status "当前文件夹不是完整的 AI-Toolbox 源码备份，无法启动。"
  exit 1
fi

PYTHON_BIN="$(find_python || true)"
if [[ -z "$PYTHON_BIN" ]]; then
  print_status "没有找到可用的 python3，无法启动本地入口。"
  exit 1
fi

IDENTITY_ROW="$(read_identity 2>/dev/null || true)"
IFS=$'\t' read -r APP_ID EXPECTED_API_VERSION BUILD_ID SOURCE_ID <<< "$IDENTITY_ROW"
if [[ -z "${APP_ID:-}" || -z "${EXPECTED_API_VERSION:-}" || -z "${BUILD_ID:-}" || -z "${SOURCE_ID:-}" ]]; then
  print_status "工作台构建身份文件无效，未启动本地入口。"
  exit 1
fi

choice="$(choose_port "$DEFAULT_PORT")"
if [[ -z "$choice" ]]; then
  print_status "4791-4799 端口都被其他服务占用，无法启动本地入口。"
  exit 1
fi

PORT="${choice%%:*}"
MODE="${choice##*:}"
URL="http://127.0.0.1:${PORT}/"

if [[ "$MODE" == "new" ]]; then
  if ! command -v npm >/dev/null 2>&1; then
    print_status "没有找到 npm。请先安装当前 Node.js LTS，再重新双击此文件。"
    exit 1
  fi

  if [[ ! -d "node_modules" ]]; then
    print_status "首次启动需要按 package-lock.json 安装前端依赖，文件只会写入本项目的 node_modules。"
    if ! npm ci --ignore-scripts --cache "$NPM_CACHE_DIR"; then
      print_status "依赖安装失败；未启动本地服务。"
      exit 1
    fi
  fi

  print_status "正在构建本地界面..."
  if ! npm run build; then
    print_status "界面构建失败；未启动本地服务。"
    exit 1
  fi

  print_status "正在启动 AI-Toolbox 工作台本地入口..."
  print_status "浏览器会自动打开。使用工作台时，请保持这个窗口开着。"

  (
    for _ in {1..80}; do
      if [[ "$(health_state "$PORT")" == "exact" ]]; then
        if open "$URL"; then
          print_status "AI-Toolbox 工作台已打开：$URL"
        else
          print_status "AI-Toolbox 工作台已启动，但浏览器没有自动打开。"
          print_status "请在浏览器打开：$URL"
        fi
        exit 0
      fi
      sleep 0.25
    done
  ) &

  PID_FILE="$(pid_file_for_port "$PORT")"
  umask 077
  print -r -- "$$" > "$PID_FILE"
  PYTHONDONTWRITEBYTECODE=1 exec "$PYTHON_BIN" -B app.py serve --port "$PORT"
fi

if open "$URL"; then
  print_status "AI-Toolbox 工作台已打开：$URL"
else
  print_status "检测到同一源码与构建的 AI-Toolbox 已在运行，但浏览器没有自动打开。"
  print_status "请在浏览器打开：$URL"
fi
print_status "已核对应用、API、构建与源码目录身份；这个窗口可以关闭。"
