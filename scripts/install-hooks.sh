#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

GIT_COMMON_DIR="$(git rev-parse --git-common-dir)"
if [[ "$GIT_COMMON_DIR" = /* ]]; then
    HOOK_DIR="${GIT_COMMON_DIR}/hooks"
else
    HOOK_DIR="${REPO_ROOT}/${GIT_COMMON_DIR}/hooks"
fi

install_hook() {
    local name="$1"
    local source_path="${REPO_ROOT}/scripts/${name}"
    local target_path="${HOOK_DIR}/${name}"

    if [ ! -f "$source_path" ]; then
        printf "오류: %s 파일이 없습니다.\n" "$source_path" >&2
        exit 1
    fi

    mkdir -p "$HOOK_DIR"
    if [ -e "$target_path" ] && [ ! -L "$target_path" ]; then
        local backup_path="${target_path}.bak.$(date +%Y%m%d%H%M%S)"
        mv "$target_path" "$backup_path"
        printf "기존 훅 백업: %s\n" "$backup_path"
    else
        rm -f "$target_path"
    fi

    ln -s "$source_path" "$target_path"
    printf "%s 설치 완료: %s -> %s\n" "$name" "$target_path" "$source_path"
}

install_hook "pre-commit"
install_hook "pre-push"
