#!/usr/bin/env bash
set -u

# 既存のMCPサーバープロセスを停止（あれば）
pkill -f "chrome_mcp_server.py" || true

cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3 || command -v python)"
fi
if [ -z "$PYTHON_BIN" ]; then
  echo "python executable not found"
  exit 1
fi

CONFIG_ARG="${1:-}"

resolve_bot_config() {
  local target_config="${1:-}"

  # コマンドライン引数が未指定なら環境変数をチェック
  if [ -z "$target_config" ]; then
    target_config="${OLLAMA_BOT_CONFIG:-}"
  fi

  # それもなければ .env があれば OLLAMA_BOT_CONFIG を抽出
  if [ -z "$target_config" ] && [ -f .env ]; then
    target_config="$(grep -E '^[[:space:]]*OLLAMA_BOT_CONFIG=' .env 2>/dev/null | tail -n 1 | cut -d '=' -f 2- | tr -d '"'\'' ' || true)"
  fi

  # パスや拡張子の正規化 (例: ollama_bot/config_shanks.py -> config_shanks)
  if [ -n "$target_config" ]; then
    target_config="$(basename "$target_config" .py)"
    # もし "shanks" のように config_ が省略されている場合、config_shanks.py が存在すれば補完
    if [ ! -f "ollama_bot/${target_config}.py" ] && [ -f "ollama_bot/config_${target_config}.py" ]; then
      target_config="config_${target_config}"
    fi
  fi

  # 指定された設定ファイルの実在チェック
  if [ -n "$target_config" ]; then
    if [ -f "ollama_bot/${target_config}.py" ]; then
      echo "$target_config"
      return 0
    else
      echo "Warning: Config '$target_config' (ollama_bot/${target_config}.py) not found." >&2
      target_config=""
    fi
  fi

  # 実在する config_*.py を自動検出 (base, sample, loader, test等を除外)
  local detected=()
  for cfg_path in ollama_bot/config_*.py; do
    [ -f "$cfg_path" ] || continue
    local bname
    bname="$(basename "$cfg_path" .py)"
    case "$bname" in
      config_base|config_sample|config_loader|config_test)
        continue
        ;;
      *)
        detected+=("$bname")
        ;;
    esac
  done

  if [ "${#detected[@]}" -gt 0 ]; then
    echo "Auto-detected bot config: ${detected[0]}" >&2
    echo "${detected[0]}"
    return 0
  fi

  if [ -f "ollama_bot/config_sample.py" ]; then
    echo "Warning: No specific persona config found. Falling back to config_sample." >&2
    echo "config_sample"
    return 0
  fi

  echo "Error: No valid config_*.py found in ollama_bot/." >&2
  return 1
}

"$PYTHON_BIN" ./control_single_ollama.py stop all

echo "Preserving Python byte-code cache (__pycache__ and *.pyc) for faster startup..."
# find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
# find . -type f -name "*.pyc" -delete 2>/dev/null || true

user_exists() {
  [ -n "${1:-}" ] && id -u "$1" >/dev/null 2>&1
}

resolve_bot_chrome_os_user() {
  if [ -n "${MCP_CHROME_BOT_OS_USER:-}" ]; then
    echo "$MCP_CHROME_BOT_OS_USER"
    return 0
  fi

  if [ "$(id -u)" = "0" ]; then
    if user_exists mcpchrome; then
      echo "mcpchrome"
      return 0
    fi
    if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
      echo "$SUDO_USER"
      return 0
    fi
    if user_exists archlinux; then
      echo "archlinux"
      return 0
    fi
  fi
}

home_for_os_user() {
  target_user="${1:-}"
  if [ -z "$target_user" ]; then
    echo "${HOME:-}"
    return 0
  fi

  if command -v getent >/dev/null 2>&1; then
    user_home="$(getent passwd "$target_user" | awk -F: '{print $6; exit}')"
    if [ -n "$user_home" ]; then
      echo "$user_home"
      return 0
    fi
  fi

  if command -v dscl >/dev/null 2>&1; then
    user_home="$(dscl . -read "/Users/$target_user" NFSHomeDirectory 2>/dev/null | awk '{print $2; exit}')"
    if [ -n "$user_home" ]; then
      echo "$user_home"
      return 0
    fi
  fi

  echo "${HOME:-}"
}

os_name() {
  uname -s 2>/dev/null || echo ""
}

run_open_for_os_user() {
  target_user="${1:-}"
  shift || true

  if [ -n "$target_user" ] && [ "$(id -u)" = "0" ]; then
    if ! user_exists "$target_user"; then
      echo "Configured MCP_CHROME_BOT_OS_USER does not exist: $target_user"
      return 1
    fi

    target_uid="$(id -u "$target_user")"
    if command -v launchctl >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1; then
      launchctl asuser "$target_uid" sudo -H -u "$target_user" open "$@"
      return $?
    fi
    if command -v sudo >/dev/null 2>&1; then
      sudo -H -u "$target_user" open "$@"
      return $?
    fi
  fi

  open "$@"
}

run_command_for_os_user() {
  target_user="${1:-}"
  shift || true
  env_args=()

  if [ "$(os_name)" != "Darwin" ]; then
    target_home="$(home_for_os_user "$target_user")"
    target_uid=""
    if [ -n "$target_user" ] && user_exists "$target_user"; then
      target_uid="$(id -u "$target_user")"
    fi

    display_value="${MCP_CHROME_DISPLAY:-${DISPLAY:-:0}}"
    if [ -n "$display_value" ]; then
      env_args+=("DISPLAY=$display_value")
    fi

    wayland_display_value="${MCP_CHROME_WAYLAND_DISPLAY:-${WAYLAND_DISPLAY:-}}"
    if [ -n "$wayland_display_value" ]; then
      env_args+=("WAYLAND_DISPLAY=$wayland_display_value")
    fi

    xdg_runtime_value="${MCP_CHROME_XDG_RUNTIME_DIR:-${XDG_RUNTIME_DIR:-}}"
    if [ -z "$xdg_runtime_value" ] && [ -n "$target_uid" ] && [ -d "/run/user/$target_uid" ]; then
      xdg_runtime_value="/run/user/$target_uid"
    fi
    if [ -n "$xdg_runtime_value" ]; then
      env_args+=("XDG_RUNTIME_DIR=$xdg_runtime_value")
    fi

    dbus_value="${MCP_CHROME_DBUS_SESSION_BUS_ADDRESS:-${DBUS_SESSION_BUS_ADDRESS:-}}"
    if [ -z "$dbus_value" ] && [ -n "$xdg_runtime_value" ] && [ -S "$xdg_runtime_value/bus" ]; then
      dbus_value="unix:path=$xdg_runtime_value/bus"
    fi
    if [ -n "$dbus_value" ]; then
      env_args+=("DBUS_SESSION_BUS_ADDRESS=$dbus_value")
    fi

    xauthority_value="${MCP_CHROME_XAUTHORITY:-${XAUTHORITY:-}}"
    if [ -z "$xauthority_value" ] && [ -n "$target_home" ] && [ -f "$target_home/.Xauthority" ]; then
      xauthority_value="$target_home/.Xauthority"
    fi
    if [ -n "$xauthority_value" ]; then
      env_args+=("XAUTHORITY=$xauthority_value")
    fi
  fi

  if [ -n "$target_user" ] && [ "$(id -u)" = "0" ]; then
    if ! user_exists "$target_user"; then
      echo "Configured MCP_CHROME_BOT_OS_USER does not exist: $target_user"
      return 1
    fi
    if command -v sudo >/dev/null 2>&1; then
      sudo -H -u "$target_user" env "${env_args[@]}" "$@"
      return $?
    fi
    if command -v runuser >/dev/null 2>&1; then
      runuser -u "$target_user" -- env "${env_args[@]}" "$@"
      return $?
    fi
  fi

  env "${env_args[@]}" "$@"
}

command_for_os_user() {
  target_user="${1:-}"
  command_name="${2:-}"
  if [ -z "$command_name" ]; then
    return 1
  fi

  if [ -n "$target_user" ] && [ "$(id -u)" = "0" ]; then
    if ! user_exists "$target_user"; then
      return 1
    fi
    if command -v sudo >/dev/null 2>&1; then
      sudo -H -u "$target_user" sh -lc "command -v '$command_name'" 2>/dev/null
      return $?
    fi
    if command -v runuser >/dev/null 2>&1; then
      runuser -u "$target_user" -- sh -lc "command -v '$command_name'" 2>/dev/null
      return $?
    fi
  fi

  command -v "$command_name"
}

default_mcp_chrome_extension_path() {
  target_home="${1:-}"
  if [ -n "$target_home" ] && [ -f "$target_home/mcp-chrome-extension/manifest.json" ]; then
    echo "$target_home/mcp-chrome-extension"
  fi
}

# bot専用Xアカウントは、このChromeプロファイル内で一度だけ手動ログインして使う。
# rootから実行する場合は MCP_CHROME_BOT_OS_USER=mcpchrome のようにChrome用のOSユーザを指定する。
# 必要なら MCP_CHROME_EXTENSION_PATH=/path/to/unpacked/mcp-chrome-extension を指定する。
start_mcp_chrome_bot_profile() {
  case "${MCP_CHROME_BOT_PROFILE_ENABLED:-1}" in
    1|true|TRUE|yes|YES|on|ON) ;;
    *) echo "Bot Chrome profile launch is disabled."; return 0 ;;
  esac

  BOT_CHROME_OS_USER="$(resolve_bot_chrome_os_user)"
  BOT_CHROME_HOME="$(home_for_os_user "$BOT_CHROME_OS_USER")"
  BOT_CHROME_USER_DATA_DIR="${MCP_CHROME_BOT_USER_DATA_DIR:-$BOT_CHROME_HOME/.discord-ai-bot/mcp-chrome-x-profile}"
  BOT_CHROME_STARTUP_URL="${MCP_CHROME_BOT_STARTUP_URL:-https://x.com/home}"
  BOT_CHROME_APP="${MCP_CHROME_BOT_APP:-Google Chrome}"
  BOT_CHROME_EXTENSION_PATH="${MCP_CHROME_EXTENSION_PATH:-$(default_mcp_chrome_extension_path "$BOT_CHROME_HOME")}"
  BOT_CHROME_DEFAULT_EXTENSION_PATH="$(default_mcp_chrome_extension_path "$BOT_CHROME_HOME")"
  BOT_CHROME_REMOTE_DEBUGGING_PORT="${MCP_CHROME_REMOTE_DEBUGGING_PORT:-}"

  if [ -n "$BOT_CHROME_OS_USER" ] && [ "$(id -u)" = "0" ]; then
    run_command_for_os_user "$BOT_CHROME_OS_USER" mkdir -p "$BOT_CHROME_USER_DATA_DIR" || return 1
  else
    mkdir -p "$BOT_CHROME_USER_DATA_DIR"
  fi

  chrome_args=(
    "--user-data-dir=$BOT_CHROME_USER_DATA_DIR"
    "--no-first-run"
    "--no-default-browser-check"
  )
  if [ -n "$BOT_CHROME_EXTENSION_PATH" ]; then
    if [ ! -f "$BOT_CHROME_EXTENSION_PATH/manifest.json" ]; then
      echo "Configured MCP_CHROME_EXTENSION_PATH does not contain manifest.json: $BOT_CHROME_EXTENSION_PATH"
      if [ -n "$BOT_CHROME_DEFAULT_EXTENSION_PATH" ]; then
        BOT_CHROME_EXTENSION_PATH="$BOT_CHROME_DEFAULT_EXTENSION_PATH"
        echo "Falling back to default mcp-chrome extension path: $BOT_CHROME_EXTENSION_PATH"
      else
        BOT_CHROME_EXTENSION_PATH=""
      fi
    fi
  fi
  if [ -n "$BOT_CHROME_EXTENSION_PATH" ]; then
    chrome_args+=("--load-extension=$BOT_CHROME_EXTENSION_PATH")
  fi
  if [ -n "$BOT_CHROME_REMOTE_DEBUGGING_PORT" ]; then
    chrome_args+=("--remote-debugging-port=$BOT_CHROME_REMOTE_DEBUGGING_PORT")
  fi
  if [ -n "$BOT_CHROME_STARTUP_URL" ]; then
    chrome_args+=("$BOT_CHROME_STARTUP_URL")
  fi

  if ps -ax -o command= | grep -F -- "--user-data-dir=$BOT_CHROME_USER_DATA_DIR" | grep -v -F -- "grep -F" >/dev/null 2>&1; then
    echo "Bot Chrome profile already appears to be running: $BOT_CHROME_USER_DATA_DIR"
    return 0
  fi

  echo "Starting bot Chrome profile for mcp-chrome: $BOT_CHROME_USER_DATA_DIR"
  if [ -n "$BOT_CHROME_OS_USER" ]; then
    echo "Launching Chrome in OS user session: $BOT_CHROME_OS_USER"
  fi
  echo "Use this profile for the bot-only X account. First setup: connect the mcp-chrome extension and log in to X here."
  if [ -n "$BOT_CHROME_EXTENSION_PATH" ]; then
    echo "Loading mcp-chrome extension from: $BOT_CHROME_EXTENSION_PATH"
  else
    echo "MCP_CHROME_EXTENSION_PATH is empty; the extension must already be installed in this Chrome profile."
  fi

  if [ "$(os_name)" = "Darwin" ] && command -v open >/dev/null 2>&1; then
    if ! run_open_for_os_user "$BOT_CHROME_OS_USER" -na "$BOT_CHROME_APP" --args "${chrome_args[@]}" >/dev/null 2>&1; then
      echo "Failed to launch Chrome app '$BOT_CHROME_APP'. Set MCP_CHROME_BOT_APP or launch the bot profile manually."
      return 1
    fi
    return 0
  fi

  for command_name in google-chrome-stable google-chrome chromium chromium-browser; do
    chrome_bin="$(command_for_os_user "$BOT_CHROME_OS_USER" "$command_name" || true)"
    if [ -z "$chrome_bin" ]; then
      chrome_bin="$(command -v "$command_name" || true)"
    fi
    if [ -n "$chrome_bin" ]; then
      run_command_for_os_user "$BOT_CHROME_OS_USER" "$chrome_bin" "${chrome_args[@]}" >/dev/null 2>&1 &
      return 0
    fi
  done

  echo "Chrome executable was not found. Launch Chrome manually with --user-data-dir=$BOT_CHROME_USER_DATA_DIR."
  return 1
}

stop_mcp_chrome_bot_profile() {
  target_user="${1:-}"
  target_home="$(home_for_os_user "$target_user")"
  user_data_dir="${MCP_CHROME_BOT_USER_DATA_DIR:-$target_home/.discord-ai-bot/mcp-chrome-x-profile}"

  if [ -z "$user_data_dir" ]; then
    return 0
  fi

  pids="$(ps -ax -o pid=,command= | grep -F -- "--user-data-dir=$user_data_dir" | grep -v -F -- "grep -F" | awk '{print $1}' || true)"
  if [ -z "$pids" ]; then
    return 0
  fi

  echo "Stopping bot Chrome profile: $user_data_dir"
  echo "$pids" | xargs kill -TERM 2>/dev/null || true
  sleep "${MCP_CHROME_STOP_WAIT_SEC:-2}"

  pids="$(ps -ax -o pid=,command= | grep -F -- "--user-data-dir=$user_data_dir" | grep -v -F -- "grep -F" | awk '{print $1}' || true)"
  if [ -n "$pids" ]; then
    echo "$pids" | xargs kill -KILL 2>/dev/null || true
  fi
}

resolve_mcp_chrome_bridge_bin() {
  target_user="${1:-}"
  if [ -n "${MCP_CHROME_BRIDGE_BIN:-}" ]; then
    echo "$MCP_CHROME_BRIDGE_BIN"
    return 0
  fi

  bridge_bin="$(command_for_os_user "$target_user" mcp-chrome-bridge || true)"
  if [ -n "$bridge_bin" ]; then
    echo "$bridge_bin"
    return 0
  fi

  bridge_bin="$(command -v mcp-chrome-bridge || true)"
  if [ -n "$bridge_bin" ]; then
    echo "$bridge_bin"
  fi
}

copy_mcp_chrome_manifest_file() {
  source_manifest="${1:-}"
  target_manifest="${2:-}"

  if [ -z "$source_manifest" ] || [ -z "$target_manifest" ] || [ ! -f "$source_manifest" ]; then
    return 0
  fi

  if [ "$(id -u)" != "0" ]; then
    if [ ! -f "$target_manifest" ]; then
      echo "Native host manifest missing and current user cannot write it: $target_manifest"
    fi
    return 0
  fi

  target_dir="$(dirname "$target_manifest")"
  mkdir -p "$target_dir" || return 1
  cp "$source_manifest" "$target_manifest" || return 1
  chmod 644 "$target_manifest" || true
  echo "Synced Native Messaging host manifest: $target_manifest"
}

ensure_mcp_chrome_native_manifests() {
  target_user="${1:-}"
  target_home="$(home_for_os_user "$target_user")"

  if [ "$(os_name)" = "Darwin" ]; then
    return 0
  fi

  chrome_user_manifest="$target_home/.config/google-chrome/NativeMessagingHosts/com.chromemcp.nativehost.json"
  chromium_user_manifest="$target_home/.config/chromium/NativeMessagingHosts/com.chromemcp.nativehost.json"
  user_data_dir="${MCP_CHROME_BOT_USER_DATA_DIR:-$target_home/.discord-ai-bot/mcp-chrome-x-profile}"

  case "${MCP_CHROME_SYNC_SYSTEM_MANIFESTS:-1}" in
    1|true|TRUE|yes|YES|on|ON)
      copy_mcp_chrome_manifest_file "$chrome_user_manifest" "/etc/opt/chrome/native-messaging-hosts/com.chromemcp.nativehost.json" || true
      copy_mcp_chrome_manifest_file "$chromium_user_manifest" "/etc/chromium/native-messaging-hosts/com.chromemcp.nativehost.json" || true
      ;;
  esac

  case "${MCP_CHROME_SYNC_PROFILE_MANIFEST:-1}" in
    1|true|TRUE|yes|YES|on|ON)
      if [ -n "$user_data_dir" ] && [ -f "$chromium_user_manifest" ]; then
        profile_manifest="$user_data_dir/NativeMessagingHosts/com.chromemcp.nativehost.json"
        copy_mcp_chrome_manifest_file "$chromium_user_manifest" "$profile_manifest" || true
        if [ "$(id -u)" = "0" ] && [ -n "$target_user" ] && user_exists "$target_user" && [ -e "$user_data_dir/NativeMessagingHosts" ]; then
          chown -R "$target_user:$target_user" "$user_data_dir/NativeMessagingHosts" 2>/dev/null || true
        fi
      fi
      ;;
  esac
}

check_mcp_chrome_bridge() {
  target_user="${1:-}"
  bridge_bin="${2:-}"
  bridge_browser="${MCP_CHROME_BRIDGE_BROWSER:-all}"

  if [ -z "$bridge_bin" ]; then
    echo "mcp-chrome-bridge command not found for user '${target_user:-current}'."
    echo "Install it for that user, for example: sudo -H -u ${target_user:-mcpchrome} npm install -g mcp-chrome-bridge"
    return 1
  fi

  if [ "${MCP_CHROME_BRIDGE_REGISTER_ON_RESTART:-1}" = "1" ]; then
    if [ "$bridge_browser" = "detect" ]; then
      run_command_for_os_user "$target_user" "$bridge_bin" register --detect || true
    else
      run_command_for_os_user "$target_user" "$bridge_bin" register --browser "$bridge_browser" || true
    fi
  fi
  if [ "${MCP_CHROME_BRIDGE_DOCTOR_FIX_ON_RESTART:-1}" = "1" ]; then
    if [ "$bridge_browser" = "detect" ]; then
      run_command_for_os_user "$target_user" "$bridge_bin" doctor --fix || true
    else
      run_command_for_os_user "$target_user" "$bridge_bin" doctor --fix --browser "$bridge_browser" || true
    fi
  fi
  if [ "${MCP_CHROME_BRIDGE_DOCTOR_ON_RESTART:-1}" = "1" ]; then
    if [ "$bridge_browser" = "detect" ]; then
      run_command_for_os_user "$target_user" "$bridge_bin" doctor || true
    else
      run_command_for_os_user "$target_user" "$bridge_bin" doctor --browser "$bridge_browser" || true
    fi
  fi
}

wait_for_mcp_chrome() {
  if ! command -v curl >/dev/null 2>&1; then
    echo "curl not found; skipping mcp-chrome ping."
    return 0
  fi

  PING_RETRIES="${MCP_CHROME_PING_RETRIES:-6}"
  PING_DELAY_SEC="${MCP_CHROME_PING_DELAY_SEC:-2}"
  attempt=1
  while [ "$attempt" -le "$PING_RETRIES" ]; do
    if curl -fsS "$MCP_PING_URL" >/dev/null; then
      echo "mcp-chrome HTTP server is reachable: $MCP_PING_URL"
      return 0
    fi
    if [ "$attempt" -lt "$PING_RETRIES" ]; then
      sleep "$PING_DELAY_SEC"
    fi
    attempt=$((attempt + 1))
  done

  echo "mcp-chrome HTTP server is not reachable yet: $MCP_PING_URL"
  echo "Check that the bot Chrome profile has the mcp-chrome extension installed, enabled, and connected."
  echo "Open the extension popup in the bot Chrome profile and click Connect/Start so it launches the native server."
  return 1
}

mcp_chrome_protocol_check() {
  case "${MCP_CHROME_PROTOCOL_CHECK:-1}" in
    1|true|TRUE|yes|YES|on|ON) ;;
    *) echo "mcp-chrome protocol check is disabled."; return 0 ;;
  esac

  if [ ! -f "./mcp_chrome_connect_test.py" ]; then
    echo "mcp_chrome_connect_test.py not found; skipping mcp-chrome protocol check."
    return 0
  fi

  session_path="${WEB_RESEARCH_MCP_SESSION_PATH:-/tmp/discord-ai-bot-chrome-mcp-session}"
  timeout_sec="${WEB_RESEARCH_MCP_TIMEOUT_SEC:-20}"
  echo "Checking mcp-chrome MCP protocol endpoint: $MCP_URL"
  "$PYTHON_BIN" ./mcp_chrome_connect_test.py \
    --endpoint "$MCP_URL" \
    --session-path "$session_path" \
    --timeout-sec "$timeout_sec"
}

restart_mcp_chrome_bot_profile() {
  target_user="${1:-}"
  stop_mcp_chrome_bot_profile "$target_user" || true
  start_mcp_chrome_bot_profile || true
  wait_for_mcp_chrome
}

reset_mcp_chrome_extension_settings() {
  target_user="${1:-}"
  target_home="$(home_for_os_user "$target_user")"
  user_data_dir="${MCP_CHROME_BOT_USER_DATA_DIR:-$target_home/.discord-ai-bot/mcp-chrome-x-profile}"
  extension_id="${MCP_CHROME_EXTENSION_ID:-hbdgbgagpkpjffpklnamcljpakneikee}"
  settings_dir="$user_data_dir/Default/Local Extension Settings/$extension_id"

  if [ -z "$user_data_dir" ] || [ -z "$extension_id" ]; then
    return 0
  fi

  echo "Resetting mcp-chrome extension local settings: $settings_dir"
  rm -rf "$settings_dir" || true
  if [ "$(id -u)" = "0" ] && [ -n "$target_user" ] && user_exists "$target_user" && [ -e "$user_data_dir" ]; then
    chown -R "$target_user:$target_user" "$user_data_dir" 2>/dev/null || true
  fi
}

recover_mcp_chrome_after_failed_ping() {
  target_user="${1:-}"

  case "${MCP_CHROME_RESET_EXTENSION_SETTINGS_ON_FAILED_PING:-1}" in
    1|true|TRUE|yes|YES|on|ON) ;;
    *) return 1 ;;
  esac

  echo "Attempting mcp-chrome recovery: reset extension settings and restart bot Chrome profile."
  stop_mcp_chrome_bot_profile "$target_user" || true
  reset_mcp_chrome_extension_settings "$target_user" || true
  ensure_mcp_chrome_native_manifests "$target_user" || true
  start_mcp_chrome_bot_profile || true
  wait_for_mcp_chrome
}

print_mcp_chrome_diagnostics() {
  target_user="${1:-}"
  target_home="$(home_for_os_user "$target_user")"
  user_data_dir="${MCP_CHROME_BOT_USER_DATA_DIR:-$target_home/.discord-ai-bot/mcp-chrome-x-profile}"

  echo "mcp-chrome diagnostics:"
  echo "- OS user: ${target_user:-current}"
  echo "- Home: ${target_home:-unknown}"
  echo "- Chrome profile: $user_data_dir"
  echo "- DISPLAY: ${MCP_CHROME_DISPLAY:-${DISPLAY:-:0}}"
  echo "- Browser registration target: ${MCP_CHROME_BRIDGE_BROWSER:-all}"

  for manifest in \
    "$target_home/.config/google-chrome/NativeMessagingHosts/com.chromemcp.nativehost.json" \
    "$target_home/.config/chromium/NativeMessagingHosts/com.chromemcp.nativehost.json" \
    "/etc/opt/chrome/native-messaging-hosts/com.chromemcp.nativehost.json" \
    "/etc/chromium/native-messaging-hosts/com.chromemcp.nativehost.json" \
    "$user_data_dir/NativeMessagingHosts/com.chromemcp.nativehost.json"
  do
    if [ -f "$manifest" ]; then
      echo "- Native host manifest exists: $manifest"
    else
      echo "- Native host manifest missing: $manifest"
    fi
  done

  if ps -ax -o user=,pid=,command= | grep -F -- "--user-data-dir=$user_data_dir" | grep -v -F -- "grep -F" >/dev/null 2>&1; then
    echo "- Chrome profile process: running"
    ps -ax -o user=,pid=,command= | grep -F -- "--user-data-dir=$user_data_dir" | grep -v -F -- "grep -F" || true
  else
    echo "- Chrome profile process: not found"
  fi

  if command -v ss >/dev/null 2>&1; then
    ss -ltnp 2>/dev/null | grep -F ":12306" || echo "- Port 12306 listener: not found"
  elif command -v netstat >/dev/null 2>&1; then
    netstat -ltnp 2>/dev/null | grep -F ":12306" || echo "- Port 12306 listener: not found"
  else
    echo "- Port 12306 listener: skipped (ss/netstat not found)"
  fi
}

run_mcp_chrome_setup() {
  MCP_CHROME_OS_USER="$(resolve_bot_chrome_os_user)"
  MCP_CHROME_BRIDGE_BIN="$(resolve_mcp_chrome_bridge_bin "$MCP_CHROME_OS_USER")"
  echo "Using external mcp-chrome server: $MCP_URL"
  check_mcp_chrome_bridge "$MCP_CHROME_OS_USER" "$MCP_CHROME_BRIDGE_BIN" || true
  ensure_mcp_chrome_native_manifests "$MCP_CHROME_OS_USER" || true

  case "${MCP_CHROME_RESTART_PROFILE_ON_RESTART:-1}" in
    1|true|TRUE|yes|YES|on|ON)
      stop_mcp_chrome_bot_profile "$MCP_CHROME_OS_USER" || true
      ;;
  esac

  start_mcp_chrome_bot_profile || true
  if ! wait_for_mcp_chrome; then
    recover_mcp_chrome_after_failed_ping "$MCP_CHROME_OS_USER" || true
  fi
  if ! mcp_chrome_protocol_check; then
    echo "mcp-chrome ping responded, but MCP protocol check failed. Restarting bot Chrome profile once."
    if restart_mcp_chrome_bot_profile "$MCP_CHROME_OS_USER"; then
      mcp_chrome_protocol_check || true
    fi
    case "${MCP_CHROME_RESET_EXTENSION_SETTINGS_ON_FAILED_PROTOCOL_CHECK:-0}" in
      1|true|TRUE|yes|YES|on|ON)
        recover_mcp_chrome_after_failed_ping "$MCP_CHROME_OS_USER" || true
        mcp_chrome_protocol_check || true
        ;;
    esac
  fi
  print_mcp_chrome_diagnostics "$MCP_CHROME_OS_USER" || true
}

# MCP接続の確認
# mcp-chrome は Chrome 拡張が Native Messaging 経由で bridge を起動する。
# 旧Playwrightサーバは起動せず、bot専用Chromeプロファイルの起動と接続確認だけを行う。
MCP_URL="${WEB_RESEARCH_MCP_URL:-http://127.0.0.1:12306/mcp}"
MCP_PING_URL="${MCP_URL%/mcp}/ping"
run_mcp_chrome_setup 2>&1 | tee -a mcp.log

# ---------------------------------------------------------
# [投機実行 (Speculative Decoding) を行う場合の設定]
# Ollamaの代わりにllama-serverを使用する場合は、以下のコマンドを別ウィンドウで起動しておきます。
# (例: メインモデルが Qwen3.6-35B、ドラフトモデルが 1.5B の場合)
# ./llama-server \
#   -m /opt/Qwen3.6-35B-A3B-Q4_K_M/Qwen3.6-35B-A3B-Q4_K_M.gguf \
#   -md /path/to/draft-model-1.5B.gguf \
#   --port 8080 -c 8192 -ngl 99
#
# その後、config_<persona>.py 内で OLLAMA_USE_OPENAI_API=True とし、
# OLLAMA_BASE_URL="http://127.0.0.1:8080" に設定することでbotが接続します。
# ---------------------------------------------------------

BOT_CONFIG="$(resolve_bot_config "$CONFIG_ARG")"
if [ -z "$BOT_CONFIG" ]; then
  echo "Error: Failed to resolve bot config module."
  exit 1
fi

"$PYTHON_BIN" ./control_single_ollama.py start "$BOT_CONFIG"

