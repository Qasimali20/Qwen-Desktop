#!/bin/bash
# Runs /qd/cmd.sh (read-only control mount) (written by the server) with bash. stderr is merged into stdout by the caller.
export HOME=/tmp GIT_CONFIG_GLOBAL=/tmp/.gitconfig TERM=dumb PAGER=cat GIT_PAGER=cat
git config --global safe.directory '*' 2>/dev/null
git config --global user.name "Qwen" 2>/dev/null
git config --global user.email "qwen@localhost" 2>/dev/null
cd "${QD_CWD:-/work}" 2>/dev/null || cd /work
exec bash --noprofile --norc -o pipefail "${1:-/qd/cmd.sh}"
