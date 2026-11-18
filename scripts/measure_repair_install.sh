#!/usr/bin/env bash
# Resolve the real executable without the shims directory on PATH.
tool=${0##*/}
shim_dir=${0%/*}
real=$(PATH=":${PATH}:"; PATH=${PATH//":$shim_dir:"/:}; PATH=${PATH#:}; PATH=${PATH%:}; command -v "$tool") || exit 127

install=false
case "$tool:$*" in
  pip*:install|pip*:install\ *|pip*:download|pip*:download\ *|\
  python*:-m\ pip\ install|python*:-m\ pip\ install\ *|\
  python*:-m\ pip\ download|python*:-m\ pip\ download\ *|\
  uv:sync|uv:sync\ *|uv:pip\ install|uv:pip\ install\ *|uv:run|uv:run\ *|\
  npm:ci|npm:ci\ *|npm:install|npm:install\ *|npm:i|npm:i\ *) install=true ;;
esac
if ! "$install"; then
  exec "$real" "$@"
fi

started=$SECONDS
"$real" "$@"
status=$?
phase=install
if [[ "$tool" == uv && ${1:-} == run ]]; then
  phase=uv_run_including_possible_sync
fi
printf 'dependency_timing tool=%s phase=%s elapsed_seconds=%s exit_code=%s\n' \
  "$tool" "$phase" "$((SECONDS - started))" "$status" >> "${OSSCOUT_DEPENDENCY_TIMING:-/dev/null}"
exit "$status"
