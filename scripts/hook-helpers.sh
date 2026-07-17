#!/usr/bin/env bash

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

info() {
    printf "%b[%s]%b %s\n" "$GREEN" "$HOOK_NAME" "$NC" "$*"
}

warn() {
    printf "%b[%s]%b %s\n" "$YELLOW" "$HOOK_NAME" "$NC" "$*"
}

fail() {
    printf "%b[%s]%b %s\n" "$RED" "$HOOK_NAME" "$NC" "$*" >&2
    exit 1
}
