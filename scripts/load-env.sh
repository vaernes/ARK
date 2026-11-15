#!/usr/bin/env bash

load_ark_env() {
  local env_file="$1" key value parser_pid parser_fd
  coproc ARK_ENV_PARSER { python3 "${SCRIPT_DIR}/parse_env.py" "${env_file}"; }
  parser_pid="${ARK_ENV_PARSER_PID}"
  parser_fd="${ARK_ENV_PARSER[0]}"
  while IFS= read -r -d '' key <&"${parser_fd}"; do
    if ! IFS= read -r -d '' value <&"${parser_fd}"; then
      echo "ERROR: Incomplete environment data from ${env_file}." >&2
      wait "${parser_pid}" || true
      return 1
    fi
    export "${key}=${value}" || return 1
  done
  wait "${parser_pid}"
}
