#!/bin/zsh

set -u

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_PORT="${TOOLBOX_PORT:-4791}"
NPM_CACHE_DIR="${TMPDIR:-/tmp}/ai-toolbox-npm-cache-${UID}"
RELEASE_ID="0.2.0-public"
LOCK_STAMP="$ROOT_DIR/.ai-toolbox-lock.sha256"

print_status() {
  echo ""
  echo "$1"
}

is_toolbox_response() {
  local port="$1"
  local body

  body="$(curl -fsS --max-time 1 "http://127.0.0.1:${port}/api/health" 2>/dev/null || true)"
  [[ "$body" == *"\"app\":\"ai-toolbox-workbench\""* && "$body" == *"\"release_id\":\"${RELEASE_ID}\""* ]]
}

is_port_busy() {
  local port="$1"
  lsof -n -P -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
}

choose_port() {
  local first="$1"
  local port
  local ports

  ports=("$first" 4791 4792 4793 4794 4795)

  for port in "${ports[@]}"; do
    if is_toolbox_response "$port"; then
      echo "${port}:running"
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

find_python() {
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  return 1
}

cd "$ROOT_DIR" || exit 1

if [[ ! -f "app.py" || ! -f "package-lock.json" ]]; then
  print_status "当前文件夹不是完整的 AI-Toolbox 源码备份，无法启动。"
  exit 1
fi

PYTHON_BIN="$(find_python || true)"
if [[ -z "$PYTHON_BIN" ]]; then
  print_status "没有找到可用的 python3，无法启动本地入口。"
  exit 1
fi

choice="$(choose_port "$DEFAULT_PORT")"
if [[ -z "$choice" ]]; then
  print_status "4791-4795 端口都被占用，无法启动本地入口。"
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

  LOCK_HASH="$(shasum -a 256 package-lock.json 2>/dev/null | awk '{print $1}')"
  INSTALLED_LOCK_HASH=""
  if [[ -f "$LOCK_STAMP" ]]; then
    INSTALLED_LOCK_HASH="$(tr -d '[:space:]' < "$LOCK_STAMP")"
  fi

  if [[ ! -d "node_modules" || -z "$LOCK_HASH" || "$INSTALLED_LOCK_HASH" != "$LOCK_HASH" ]]; then
    print_status "正在按 package-lock.json 准备前端依赖，文件只会写入本项目的 node_modules。"
    if ! npm ci --ignore-scripts --cache "$NPM_CACHE_DIR"; then
      print_status "依赖安装失败；未启动本地服务。"
      exit 1
    fi
    if [[ -n "$LOCK_HASH" ]]; then
      printf '%s\n' "$LOCK_HASH" > "$LOCK_STAMP"
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
      if is_toolbox_response "$PORT"; then
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

  PYTHONDONTWRITEBYTECODE=1 exec "$PYTHON_BIN" -B app.py serve --port "$PORT"
fi

if open "$URL"; then
  print_status "AI-Toolbox 工作台已打开：$URL"
else
  print_status "检测到 AI-Toolbox 工作台已在运行，但浏览器没有自动打开。"
  print_status "请在浏览器打开：$URL"
fi
print_status "检测到本地入口已在运行，这个窗口可以关闭。"
