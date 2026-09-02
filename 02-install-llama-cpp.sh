#!/bin/bash

# Set strict error handling
set -euo pipefail

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Configuration
LLAMA_HOME="$HOME/llama.cpp"
LLAMA_STAGING="$HOME/.llama.cpp.build.$$"  # Use PID to make unique
LOCAL_BIN="$HOME/.local/bin"
GITHUB_REPO="https://github.com/ggerganov/llama.cpp.git"
LOCK_FILE="/tmp/llama-install.lock"

# Prevent re-entrance
prevent_reentrancy() {
    if [ -f "$LOCK_FILE" ]; then
        local old_pid=$(cat "$LOCK_FILE")
        if kill -0 "$old_pid" 2>/dev/null; then
            echo -e "${RED}✗ Another installation is already running (PID: $old_pid)${NC}"
            echo "Please wait for it to complete or manually remove $LOCK_FILE if stalled."
            exit 1
        fi
    fi
    
    echo $$ > "$LOCK_FILE"
    trap 'rm -f "$LOCK_FILE"' EXIT
}

# Error handling
trap 'handle_error $?' ERR
trap 'handle_interrupt' INT TERM

handle_error() {
    local exit_code=$1
    echo -e "\n${RED}✗ Build failed${NC}"
    echo -e "${YELLOW}Cleaning up staging directory...${NC}"
    rm -rf "$LLAMA_STAGING"
    echo -e "${GREEN}✓ Existing llama.cpp installation untouched${NC}"
    exit "$exit_code"
}

handle_interrupt() {
    echo -e "\n${RED}✗ Build interrupted${NC}"
    echo -e "${YELLOW}Cleaning up staging directory...${NC}"
    rm -rf "$LLAMA_STAGING"
    echo -e "${GREEN}✓ Existing llama.cpp installation untouched${NC}"
    exit 130
}

# Check prerequisites
check_prerequisites() {
    echo -e "${BLUE}Checking prerequisites...${NC}"
    
    local missing=0
    
    for cmd in git cmake make gcc g++; do
        if ! command -v "$cmd" &> /dev/null; then
            echo -e "${RED}✗ $cmd not found${NC}"
            missing=1
        else
            echo -e "${GREEN}✓ $cmd available${NC}"
        fi
    done
    
    if ! rocm-smi &> /dev/null; then
        echo -e "${YELLOW}⚠ ROCm not detected (rocm-smi not found)${NC}"
    else
        echo -e "${GREEN}✓ ROCm detected${NC}"
    fi
    
    if [ $missing -eq 1 ]; then
        echo -e "${RED}Please install missing dependencies and try again${NC}"
        exit 1
    fi
    
    echo -e "${GREEN}✓ All prerequisites met${NC}\n"
}

# Detect running llama.cpp processes (only actual binaries, not the installer)
detect_llama_processes() {
    pgrep -x "llama-cli|llama-server|llama" 2>/dev/null || true
}

# Detect llama.cpp systemd services
detect_llama_services() {
    systemctl list-units --all 2>/dev/null | grep -i llama | awk '{print $1}' || true
}

# Setup repository (safe operation - doesn't require service stop)
setup_repository() {
    echo -e "${BLUE}=== Setting up repository ===${NC}\n"
    
    if [ -d "$LLAMA_HOME/.git" ]; then
        echo -e "${YELLOW}Repository already exists. Cloning into staging area for build...${NC}"
        git clone "$LLAMA_HOME" "$LLAMA_STAGING" 2>&1 | grep -v "^Cloning into" || true
        
        cd "$LLAMA_STAGING"
        
        echo -e "${BLUE}Fetching latest updates...${NC}"
        git fetch origin || {
            echo -e "${RED}✗ Failed to fetch updates${NC}"
            return 1
        }
        
        git checkout main 2>/dev/null || git checkout master 2>/dev/null || {
            echo -e "${RED}✗ Failed to checkout branch${NC}"
            return 1
        }
        
        echo -e "${GREEN}✓ Repository updated${NC}"
    else
        echo -e "${BLUE}Cloning repository into staging area...${NC}"
        git clone "$GITHUB_REPO" "$LLAMA_STAGING" || {
            echo -e "${RED}✗ Failed to clone repository${NC}"
            return 1
        }
        
        cd "$LLAMA_STAGING"
        echo -e "${GREEN}✓ Repository cloned${NC}"
    fi
    
    echo ""
}

# Build llama.cpp in staging directory (safe operation)
build_llama() {
    echo -e "${BLUE}=== Building llama.cpp ===${NC}\n"
    
    cd "$LLAMA_STAGING"
    
    mkdir -p build
    cd build
    
    echo -e "${BLUE}Configuring with CMake (ROCm + Vulkan enabled)...${NC}"
    
    cmake -DGGML_HIP=ON \
          -DGGML_HIP_ROCWMMA_FATTN=1 \
          -DGGML_VULKAN=ON \
          -DAMDGPU_TARGETS="gfx1151" \
          -DLLAMA_NATIVE=ON \
          -DCMAKE_BUILD_TYPE=Release \
          .. || {
        echo -e "${RED}✗ CMake configuration failed${NC}"
        return 1
    }
    
    echo -e "${GREEN}✓ CMake configuration complete${NC}\n"
    
    echo -e "${BLUE}Building (this takes 10-20 minutes)...${NC}"
    local num_jobs=$(nproc)
    echo "Using $num_jobs parallel jobs"
    
    cmake --build . --config Release -j"$num_jobs" || {
        echo -e "${RED}✗ Build failed${NC}"
        return 1
    }
    
    echo -e "${GREEN}✓ Build complete${NC}\n"
}

# Verify binaries exist in staging
verify_build() {
    echo -e "${BLUE}=== Verifying build ===${NC}\n"
    
    cd "$LLAMA_STAGING/build"
    
    local found_binaries=0
    
    for binary in llama-cli llama-server llama; do
        if [ -f "bin/$binary" ] || [ -f "$binary" ]; then
            echo -e "${GREEN}✓ $binary built${NC}"
            found_binaries=1
        fi
    done
    
    if [ $found_binaries -eq 0 ]; then
        echo -e "${RED}✗ No binaries found after build${NC}"
        return 1
    fi
    
    echo ""
}

# Stop llama services/processes (only when we're ready to deploy)
stop_llama_services() {
    local processes=$(detect_llama_processes)
    local services=$(detect_llama_services)
    
    if [ -z "$processes" ] && [ -z "$services" ]; then
        echo -e "${GREEN}✓ No running llama.cpp processes or services${NC}"
        return 0
    fi
    
    echo -e "${BLUE}=== Stopping llama.cpp processes/services ===${NC}\n"
    
    # Stop systemd services
    if [ -n "$services" ]; then
        echo -e "${YELLOW}Stopping systemd services:${NC}"
        echo "$services" | while read service; do
            if systemctl is-active "$service" &>/dev/null; then
                echo "  Stopping $service..."
                systemctl stop "$service" || {
                    echo -e "${YELLOW}  ⚠ Failed to stop $service (may require elevated privileges)${NC}"
                }
            fi
        done
    fi
    
    # Stop running processes
    if [ -n "$processes" ]; then
        echo -e "${YELLOW}Stopping running processes:${NC}"
        echo "$processes" | while read pid; do
            local cmd=$(ps -p "$pid" -o comm= 2>/dev/null || echo "unknown")
            echo "  Stopping PID $pid ($cmd)..."
            kill -TERM "$pid" 2>/dev/null || true
        done
        
        sleep 2
        echo "$processes" | xargs -r kill -9 2>/dev/null || true
    fi
    
    echo -e "${GREEN}✓ Processes stopped${NC}\n"
}

# Install/deploy the built binaries (simple copy approach)
deploy_binaries() {
    echo -e "${BLUE}=== Deploying binaries ===${NC}\n"
    
    mkdir -p "$LOCAL_BIN"
    
    cd "$LLAMA_STAGING/build"
    
    for binary in llama-cli llama-server llama; do
        local src_path=""
        
        if [ -f "bin/$binary" ]; then
            src_path="bin/$binary"
        elif [ -f "$binary" ]; then
            src_path="$binary"
        fi
        
        if [ -n "$src_path" ] && [ -f "$src_path" ]; then
            cp -f "$src_path" "$LOCAL_BIN/$binary"
            chmod +x "$LOCAL_BIN/$binary"
            echo -e "${GREEN}✓ Deployed $binary${NC}"
        fi
    done
    
    echo ""
}

# Update persistent PATH in shell configs
setup_persistent_path() {
    echo -e "${BLUE}Setting up persistent PATH...${NC}\n"
    
    local shells=(".bashrc" ".zshrc" ".bash_profile")
    
    for shell_rc in "${shells[@]}"; do
        local rc_file="$HOME/$shell_rc"
        if [ -f "$rc_file" ]; then
            if ! grep -q "/.local/bin" "$rc_file"; then
                echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$rc_file"
                echo -e "${GREEN}✓ Updated $shell_rc${NC}"
            fi
        fi
    done
    
    echo ""
}

# Verify installation
verify_installation() {
    echo -e "${BLUE}=== Verification ===${NC}\n"
    
    export PATH="$LOCAL_BIN:$PATH"
    
    local found=0
    
    for binary in llama-cli llama-server llama; do
        if [ -f "$LOCAL_BIN/$binary" ]; then
            echo -e "${GREEN}✓ $binary${NC}"
            "$LOCAL_BIN/$binary" --version 2>&1 | head -1 || true
            found=1
        fi
    done
    
    if [ $found -eq 0 ]; then
        echo -e "${RED}✗ No binaries installed${NC}"
        return 1
    fi
    
    echo ""
}

# Main execution
main() {
    prevent_reentrancy
    
    echo -e "${BLUE}=== llama.cpp Installation & Build ===${NC}\n"
    
    check_prerequisites
    
    # All safe - no impact to existing environment
    setup_repository
    build_llama
    verify_build
    
    # NOW we stop services and deploy
    stop_llama_services
    deploy_binaries
    setup_persistent_path
    verify_installation
    
    # Cleanup staging directory
    echo -e "${BLUE}Cleaning up staging directory...${NC}"
    rm -rf "$LLAMA_STAGING"
    echo -e "${GREEN}✓ Cleanup complete${NC}\n"
    
    # Final summary
    echo -e "${GREEN}=== ✓✓✓ Installation Successful! ✓✓✓ ===${NC}\n"
    echo "llama.cpp has been updated and deployed to: $LOCAL_BIN"
    echo ""
    echo "Available commands:"
    export PATH="$LOCAL_BIN:$PATH"
    command -v llama-cli &>/dev/null && echo "  • llama-cli"
    command -v llama-server &>/dev/null && echo "  • llama-server"
    command -v llama &>/dev/null && echo "  • llama"
    echo ""
    echo "To apply PATH changes to your current shell:"
    echo "  source ~/.bashrc  (or .zshrc, .bash_profile)"
}

main "$@"

