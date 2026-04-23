#!/usr/bin/env bash

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
NC='\033[0m'

DEFAULT_REPO_URL="https://github.com/NousResearch/hermes-agent.git"
DEFAULT_INSTALL_DIR="${HERMES_INSTALL_DIR:-$HOME/.hermes/hermes-agent}"
PYTHON_VERSION="${HERMES_PYTHON_VERSION:-3.11}"
INSTALL_DIR="$DEFAULT_INSTALL_DIR"
REPO_URL=""
BRANCH=""
EXTRAS=""
MODE="minimal"
RUN_SETUP=false
SKIP_SYSTEM_DEPS=false
WITH_NODE=false
USE_CURRENT_REPO="auto"

OS="unknown"
DISTRO="unknown"
UV_CMD=""
CURRENT_REPO_ROOT=""
SOURCE_DIR=""

print_banner() {
    echo ""
    echo -e "${MAGENTA}┌─────────────────────────────────────────────────────────┐${NC}"
    echo -e "${MAGENTA}│        Hermes Agent Source Deployment Helper            │${NC}"
    echo -e "${MAGENTA}└─────────────────────────────────────────────────────────┘${NC}"
    echo ""
}

log_info() {
    echo -e "${CYAN}→${NC} $1"
}

log_success() {
    echo -e "${GREEN}✓${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}⚠${NC} $1"
}

log_error() {
    echo -e "${RED}✗${NC} $1"
}

usage() {
    cat <<'EOF'
Hermes Agent source deployment helper

Works on macOS and Ubuntu-style Linux for source installs of Hermes Agent.

Usage:
  ./scripts/deploy-source.sh [options]

Options:
  --repo-url URL        Clone/update from this repo URL instead of using the
                        current checkout. Defaults to upstream Hermes if you
                        are not already inside a Hermes checkout.
  --branch NAME         Branch to checkout/update.
  --install-dir PATH    Target checkout directory when cloning/updating.
                        Default: ~/.hermes/hermes-agent
  --mode minimal|full   Install core package only, or all extras. Default:
                        minimal
  --extras LIST         Install a custom extras list, e.g. messaging,mcp,cron
                        Overrides --mode when provided.
  --run-setup           Run `hermes setup` after installation.
  --with-node           Install Node.js when possible and run npm install for
                        optional browser/WhatsApp assets.
  --skip-system-deps    Do not try to install system packages.
  --use-current-repo    Force using the current Hermes checkout.
  --no-current-repo     Force cloning/updating into --install-dir.
  -h, --help            Show this help.

Examples:
  ./scripts/deploy-source.sh --use-current-repo
  ./scripts/deploy-source.sh --repo-url git@github.com:you/hermes-agent.git --branch feature-x
  ./scripts/deploy-source.sh --repo-url https://github.com/you/hermes-agent.git --branch feature-x --extras messaging,mcp,cron,pty
EOF
}

run_elevated() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        return 1
    fi
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --repo-url)
                REPO_URL="$2"
                shift 2
                ;;
            --branch)
                BRANCH="$2"
                shift 2
                ;;
            --install-dir)
                INSTALL_DIR="$2"
                shift 2
                ;;
            --mode)
                MODE="$2"
                shift 2
                ;;
            --extras)
                EXTRAS="$2"
                shift 2
                ;;
            --run-setup)
                RUN_SETUP=true
                shift
                ;;
            --with-node)
                WITH_NODE=true
                shift
                ;;
            --skip-system-deps)
                SKIP_SYSTEM_DEPS=true
                shift
                ;;
            --use-current-repo)
                USE_CURRENT_REPO="yes"
                shift
                ;;
            --no-current-repo)
                USE_CURRENT_REPO="no"
                shift
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                log_error "Unknown option: $1"
                usage
                exit 1
                ;;
        esac
    done

    case "$MODE" in
        minimal|full)
            ;;
        *)
            log_error "--mode must be one of: minimal, full"
            exit 1
            ;;
    esac
}

detect_os() {
    case "$(uname -s)" in
        Linux*)
            OS="linux"
            if [ -f /etc/os-release ]; then
                # shellcheck disable=SC1091
                . /etc/os-release
                DISTRO="${ID:-linux}"
            fi
            ;;
        Darwin*)
            OS="macos"
            DISTRO="macos"
            ;;
        *)
            OS="unknown"
            DISTRO="unknown"
            ;;
    esac

    log_success "Detected OS: $OS ($DISTRO)"
}

install_system_deps() {
    if [ "$SKIP_SYSTEM_DEPS" = true ]; then
        log_info "Skipping system dependency installation"
        return 0
    fi

    if [ "$OS" = "linux" ] && command -v apt-get >/dev/null 2>&1; then
        local packages=(git curl ffmpeg build-essential)
        if [ "$WITH_NODE" = true ]; then
            packages+=(nodejs npm)
        fi

        log_info "Installing system packages with apt-get when available"
        if run_elevated apt-get update && run_elevated apt-get install -y "${packages[@]}"; then
            log_success "System packages installed"
        else
            log_warn "Could not install apt packages automatically; continuing"
        fi
        return 0
    fi

    if [ "$OS" = "macos" ]; then
        if ! xcode-select -p >/dev/null 2>&1; then
            log_warn "Xcode Command Line Tools not detected. If builds fail, run: xcode-select --install"
        fi

        if command -v brew >/dev/null 2>&1; then
            local packages=()
            if ! command -v git >/dev/null 2>&1; then
                packages+=(git)
            fi
            if ! command -v ffmpeg >/dev/null 2>&1; then
                packages+=(ffmpeg)
            fi
            if [ "$WITH_NODE" = true ] && ! command -v node >/dev/null 2>&1; then
                packages+=(node)
            fi

            if [ "${#packages[@]}" -gt 0 ]; then
                log_info "Installing Homebrew packages: ${packages[*]}"
                brew install "${packages[@]}" || log_warn "Homebrew install failed for one or more packages"
            else
                log_success "Required Homebrew packages already available"
            fi
        else
            log_warn "Homebrew not found; skipping macOS package installation"
        fi
    fi
}

ensure_required_commands() {
    local missing=()
    for cmd in git curl; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            missing+=("$cmd")
        fi
    done

    if [ "${#missing[@]}" -gt 0 ]; then
        log_error "Missing required commands: ${missing[*]}"
        exit 1
    fi
}

ensure_uv() {
    log_info "Checking uv"

    if command -v uv >/dev/null 2>&1; then
        UV_CMD="uv"
    elif [ -x "$HOME/.local/bin/uv" ]; then
        UV_CMD="$HOME/.local/bin/uv"
    elif [ -x "$HOME/.cargo/bin/uv" ]; then
        UV_CMD="$HOME/.cargo/bin/uv"
    else
        log_info "Installing uv"
        # Pin installer version + SHA256 so a compromised upstream can't
        # silently rewrite our install step.  To bump: update URL, recompute
        # SHA, update both defaults below.  Opt-outs (NOT recommended):
        #   UV_INSTALL_SKIP_SHA=1  — skip checksum verification
        #   UV_INSTALL_URL=...     — override URL (then also provide a SHA)
        local uv_installer_url="${UV_INSTALL_URL:-https://astral.sh/uv/0.5.14/install.sh}"
        local uv_installer_sha="${UV_INSTALL_SHA256:-c872e0484d02c0d19e524bd4b1f3ee0a8362b363a0793dce8d4fc34a32d42679}"
        local tmp_installer
        tmp_installer="$(mktemp -t uv-install.XXXXXX.sh)"
        trap 'rm -f "$tmp_installer"' RETURN
        if ! curl -fsSL "$uv_installer_url" -o "$tmp_installer"; then
            log_error "Failed to download uv installer from $uv_installer_url"
            exit 1
        fi
        if [ "${UV_INSTALL_SKIP_SHA:-0}" = "1" ]; then
            log_warn "UV_INSTALL_SKIP_SHA=1 set — skipping installer checksum verification"
        else
            if [ -z "$uv_installer_sha" ]; then
                log_error "No UV_INSTALL_SHA256 configured and UV_INSTALL_SKIP_SHA not set; refusing to run unverified installer"
                exit 1
            fi
            local actual_sha
            actual_sha="$(shasum -a 256 "$tmp_installer" | awk '{print $1}')"
            if [ "$actual_sha" != "$uv_installer_sha" ]; then
                log_error "uv installer SHA256 mismatch: got $actual_sha, expected $uv_installer_sha"
                exit 1
            fi
        fi
        sh "$tmp_installer"
        if [ -x "$HOME/.local/bin/uv" ]; then
            UV_CMD="$HOME/.local/bin/uv"
        elif [ -x "$HOME/.cargo/bin/uv" ]; then
            UV_CMD="$HOME/.cargo/bin/uv"
        elif command -v uv >/dev/null 2>&1; then
            UV_CMD="uv"
        else
            log_error "uv installation completed but the binary was not found"
            exit 1
        fi
    fi

    log_success "Using uv: $($UV_CMD --version)"
}

ensure_python() {
    log_info "Ensuring Python $PYTHON_VERSION via uv"
    if ! "$UV_CMD" python find "$PYTHON_VERSION" >/dev/null 2>&1; then
        "$UV_CMD" python install "$PYTHON_VERSION"
    fi
    log_success "Python ready"
}

detect_current_repo_root() {
    local top
    top="$(git rev-parse --show-toplevel 2>/dev/null || true)"
    if [ -n "$top" ] && [ -f "$top/pyproject.toml" ] && grep -q 'name = "hermes-agent"' "$top/pyproject.toml"; then
        CURRENT_REPO_ROOT="$top"
    fi
}

ensure_checkout() {
    detect_current_repo_root

    if [ "$USE_CURRENT_REPO" = "yes" ]; then
        if [ -z "$CURRENT_REPO_ROOT" ]; then
            log_error "--use-current-repo was set, but the current directory is not inside a Hermes checkout"
            exit 1
        fi
        SOURCE_DIR="$CURRENT_REPO_ROOT"
        log_success "Using current checkout: $SOURCE_DIR"
    elif [ "$USE_CURRENT_REPO" = "auto" ] && [ -z "$REPO_URL" ] && [ -n "$CURRENT_REPO_ROOT" ]; then
        SOURCE_DIR="$CURRENT_REPO_ROOT"
        log_success "Using current checkout: $SOURCE_DIR"
    else
        local effective_repo_url="$REPO_URL"
        if [ -z "$effective_repo_url" ]; then
            effective_repo_url="$DEFAULT_REPO_URL"
        fi

        if [ -d "$INSTALL_DIR/.git" ]; then
            log_info "Updating checkout in $INSTALL_DIR"
            if [ -n "$REPO_URL" ]; then
                git -C "$INSTALL_DIR" remote set-url origin "$effective_repo_url"
            fi
            git -C "$INSTALL_DIR" fetch --all --tags
            # `pull --ff-only` failures mean the deployed tree won't match
            # the requested remote state.  Default to fail-closed; set
            # DEPLOY_STRICT_PULL=0 to tolerate failures (e.g. offline work
            # or intentionally diverged local commits).
            local pull_target_ref=""
            if [ -n "$BRANCH" ]; then
                git -C "$INSTALL_DIR" checkout "$BRANCH"
                pull_target_ref="origin $BRANCH"
                if ! git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"; then
                    log_warn "git pull --ff-only origin $BRANCH failed (diverged or offline)"
                    [ "${DEPLOY_STRICT_PULL:-1}" = "1" ] && exit 1
                fi
            else
                # Resolve the current branch and pull from origin explicitly,
                # so a reused checkout that tracks some non-origin upstream
                # cannot silently deploy from the wrong remote.
                local current_branch
                current_branch="$(git -C "$INSTALL_DIR" branch --show-current 2>/dev/null || true)"
                if [ -z "$current_branch" ]; then
                    log_error "Could not determine current branch in $INSTALL_DIR (detached HEAD?); pass --branch explicitly"
                    exit 1
                fi
                pull_target_ref="origin $current_branch"
                if ! git -C "$INSTALL_DIR" pull --ff-only origin "$current_branch"; then
                    log_warn "git pull --ff-only origin $current_branch failed (diverged or offline)"
                    [ "${DEPLOY_STRICT_PULL:-1}" = "1" ] && exit 1
                fi
            fi
        elif [ -e "$INSTALL_DIR" ]; then
            log_error "Install dir exists but is not a git checkout: $INSTALL_DIR"
            exit 1
        else
            mkdir -p "$(dirname "$INSTALL_DIR")"
            log_info "Cloning $effective_repo_url into $INSTALL_DIR"
            if [ -n "$BRANCH" ]; then
                git clone --branch "$BRANCH" --single-branch "$effective_repo_url" "$INSTALL_DIR"
            else
                git clone "$effective_repo_url" "$INSTALL_DIR"
            fi
        fi

        SOURCE_DIR="$INSTALL_DIR"
    fi

    if [ -n "$BRANCH" ] && [ "$SOURCE_DIR" = "$CURRENT_REPO_ROOT" ]; then
        local current_branch
        current_branch="$(git -C "$SOURCE_DIR" branch --show-current 2>/dev/null || true)"
        if [ "$current_branch" != "$BRANCH" ]; then
            if git -C "$SOURCE_DIR" diff --quiet --ignore-submodules HEAD -- 2>/dev/null; then
                log_info "Checking out branch $BRANCH in current repo"
                git -C "$SOURCE_DIR" checkout "$BRANCH"
            else
                log_error "Current checkout has uncommitted changes; refusing to switch to $BRANCH"
                exit 1
            fi
        fi
    fi

    log_success "Source directory ready: $SOURCE_DIR"
}

install_python_package() {
    log_info "Creating virtual environment in $SOURCE_DIR/venv"
    "$UV_CMD" venv "$SOURCE_DIR/venv" --python "$PYTHON_VERSION"

    # shellcheck disable=SC1090
    source "$SOURCE_DIR/venv/bin/activate"

    local install_spec="."
    if [ -n "$EXTRAS" ]; then
        install_spec=".[${EXTRAS}]"
    elif [ "$MODE" = "full" ]; then
        install_spec=".[all]"
    fi

    log_info "Installing Hermes package with spec: $install_spec"
    (
        cd "$SOURCE_DIR"
        "$UV_CMD" pip install -e "$install_spec"
    )

    log_success "Python package installed"
}

install_node_assets() {
    if [ "$WITH_NODE" != true ]; then
        return 0
    fi

    if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
        log_warn "Node.js/npm not found; skipping npm-based optional assets"
        return 0
    fi

    log_info "Installing optional npm assets"
    if [ -f "$SOURCE_DIR/package.json" ]; then
        (
            cd "$SOURCE_DIR"
            npm install
        ) || log_warn "Root npm install failed; browser tools may not work"
    fi

    if [ -f "$SOURCE_DIR/scripts/whatsapp-bridge/package.json" ]; then
        (
            cd "$SOURCE_DIR/scripts/whatsapp-bridge"
            npm install
        ) || log_warn "WhatsApp bridge npm install failed"
    fi

    log_success "Optional npm assets processed"
}

link_binaries() {
    mkdir -p "$HOME/.local/bin"

    for bin_name in hermes hermes-agent hermes-acp; do
        if [ -x "$SOURCE_DIR/venv/bin/$bin_name" ]; then
            ln -sf "$SOURCE_DIR/venv/bin/$bin_name" "$HOME/.local/bin/$bin_name"
        fi
    done

    log_success "Linked Hermes commands into ~/.local/bin"
}

maybe_run_setup() {
    if [ "$RUN_SETUP" != true ]; then
        return 0
    fi

    log_info "Running hermes setup"
    "$SOURCE_DIR/venv/bin/hermes" setup
}

print_summary() {
    local branch_text
    branch_text="$(git -C "$SOURCE_DIR" branch --show-current 2>/dev/null || echo unknown)"

    echo ""
    echo -e "${GREEN}Deployment complete.${NC}"
    echo ""
    echo "Source:      $SOURCE_DIR"
    echo "Branch:      $branch_text"
    echo "Python:      $PYTHON_VERSION"
    echo "Install:     $MODE${EXTRAS:+ (extras: $EXTRAS)}"
    echo "Node assets: $WITH_NODE"
    echo ""
    echo "Next steps:"
    echo "  source ~/.bashrc   # or ~/.zshrc if needed"
    echo "  hermes setup"
    echo "  hermes"
    echo ""
    echo "If ~/.local/bin is not on PATH, run Hermes directly:"
    echo "  $SOURCE_DIR/venv/bin/hermes"
}

main() {
    parse_args "$@"
    print_banner
    detect_os
    install_system_deps
    ensure_required_commands
    ensure_uv
    ensure_python
    ensure_checkout
    install_python_package
    install_node_assets
    link_binaries
    maybe_run_setup
    print_summary
}

main "$@"
