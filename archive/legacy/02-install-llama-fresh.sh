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
LLAMA_BACKUP="$HOME/llama.cpp.backup.$(date +%s)"
LOCAL_BIN="$HOME/.local/bin"
GITHUB_REPO="https://github.com/ggerganov/llama.cpp.git"

# Error handling
trap 'handle_error' ERR
trap 'handle_interrupt' INT TERM

handle_error() {
    local line=$1
    echo -e "\n${RED}✗ Error occurred at line $line${NC}"
    echo -e "${YELLOW}Attempting cleanup and rollback...${NC}"
    cleanup_on_error
    exit 1
}

handle_interrupt() {
    echo -e "\n${RED}✗ Build interrupted${NC}"
    echo -e "${YELLOW}Attempting cleanup...${NC}"
    cleanup_on_error
    exit 130
}

cleanup_on_error() {
    # Kill any build processes
    if [ -d "$LLAMA_HOME/build" ]; then
        pkill -f "cmake --build" || true
        pkill -f "c++" || true
    fi
    
    # Restore backup if it exists and original was damaged
    if [ -d "$LLAMA_BACKUP" ] && [ -d "$LLAMA_HOME" ]; then
        echo -e "${YELLOW}Restoring backup...${NC}"
        rm -rf "$LLAMA_HOME"
        mv "$LLAMA_BACKUP" "$LLAMA_HOME"
        echo -e "${GREEN}✓ Backup restored${NC}"
    fi
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
    
    # Check for ROCm
    if ! rocm-smi &> /dev/null; then
        echo -e "${YELLOW}⚠ ROCm not detected (rocm-smi not found)${NC}"
        echo -e "${YELLOW}  Make sure ROCm is installed and in PATH${NC}"
    else
        echo -e "${GREEN}✓ ROCm detected${NC}"
    fi
    
    if [ $missing -eq 1 ]; then
        echo -e "${RED}Please install missing dependencies and try again${NC}"
        exit 1
    fi
    
    echo -e "${GREEN}✓ All prerequisites met${NC}\n"
}

# Stop running llama.cpp processes
stop_llama_processes() {
    echo -e "${BLUE}Checking for running llama.cpp processes...${NC}"
    
    local pids=$(pgrep -f "llama-cli|llama-server|llama" || true)
    
    if [ -n "$pids" ]; then
        echo -e "${YELLOW}Found running llama processes:${NC}"
        echo "$pids" | while read pid; do
            echo "  PID $pid: $(ps -p $pid -o comm=)"
        done
        
        echo -e "${YELLOW}Attempting to stop processes gracefully...${NC}"
        echo "$pids" | xargs -r kill -TERM 2>/dev/null || true
        
        # Wait a bit for graceful shutdown
        sleep 2
        
        # Force kill if still running
        echo "$pids" | xargs -r kill -9 2>/dev/null || true
        
        echo -e "${GREEN}✓ Processes stopped${NC}"
    else
        echo -e "${GREEN}✓ No running llama processes${NC}"
    fi
    echo ""
}

# Update or clone repository
setup_repository() {
    echo -e "${BLUE}=== Setting up repository ===${NC}\n"
    
    if [ -d "$LLAMA_HOME" ]; then
        echo -e "${YELLOW}Repository already exists. Pulling updates...${NC}"
        cd "$LLAMA_HOME"
        
        # Check for uncommitted changes
        if ! git diff-index --quiet HEAD --; then
            echo -e "${YELLOW}⚠ Uncommitted changes detected. Stashing...${NC}"
            git stash
        fi
        
        # Pull latest
        git pull origin main 2>&1 || git pull origin master 2>&1 || {
            echo -e "${RED}✗ Failed to pull updates${NC}"
            return 1
        }
        
        echo -e "${GREEN}✓ Repository updated${NC}"
    else
        echo -e "${BLUE}Cloning repository...${NC}"
        git clone "$GITHUB_REPO" "$LLAMA_HOME"
        cd "$LLAMA_HOME"
        echo -e "${GREEN}✓ Repository cloned${NC}"
    fi
    
    echo ""
}

# Build llama.cpp
build_llama() {
    echo -e "${BLUE}=== Building llama.cpp ===${NC}\n"
    
    cd "$LLAMA_HOME"
    
    # Backup existing build if it exists
    if [ -d "build" ]; then
        echo -e "${YELLOW}Backing up existing build directory...${NC}"
        rm -rf build.backup
        mv build build.backup
        echo -e "${GREEN}✓ Build backed up${NC}"
    fi
    
    # Create fresh build directory
    mkdir -p build
    cd build
    
    # Configure with CMake
    echo -e "${BLUE}Configuring with CMake (ROCm + Vulkan enabled)...${NC}"
    
    cmake -DGGML_HIP=ON \
          -DGGML_HIP_ROCWMMA_FATTN=1 \
          -DGGML_VULKAN=ON \
          -DAMDGPU_TARGETS="gfx1151" \
          -DLLAMA_NATIVE=ON \
          -DCMAKE_INSTALL_PREFIX="$HOME/.local" \
          -DCMAKE_BUILD_TYPE=Release \
          .. || {
        echo -e "${RED}✗ CMake configuration failed${NC}"
        return 1
    }
    
    echo -e "${GREEN}✓ CMake configuration complete${NC}\n"
    
    # Build
    echo -e "${BLUE}Building (this takes 10-20 minutes)...${NC}"
    local num_jobs=$(nproc)
    echo "Using $num_jobs parallel jobs"
    
    cmake --build . --config Release -j"$num_jobs" || {
        echo -e "${RED}✗ Build failed${NC}"
        return 1
    }
    
    echo -e "${GREEN}✓ Build complete${NC}\n"
}

# Install binaries
install_binaries() {
    echo -e "${BLUE}=== Installing binaries ===${NC}\n"
    
    cd "$LLAMA_HOME/build"
    
    # Create installation directory
    mkdir -p "$LOCAL_BIN"
    
    # Install using cmake
    echo -e "${BLUE}Installing with CMake...${NC}"
    cmake --install . || {
        echo -e "${YELLOW}⚠ CMake install failed, using manual symlinks${NC}"
    }
    
    # Verify binaries and create symlinks
    local found_binaries=0
    
    for binary in llama-cli llama-server llama; do
        local src_path=""
        
        # Check multiple possible locations
        if [ -f "bin/$binary" ]; then
            src_path="$(pwd)/bin/$binary"
        elif [ -f "$binary" ]; then
            src_path="$(pwd)/$binary"
        fi
        
        if [ -n "$src_path" ] && [ -f "$src_path" ]; then
            ln -sf "$src_path" "$LOCAL_BIN/$binary"
            echo -e "${GREEN}✓ Installed $binary${NC}"
            found_binaries=1
        fi
    done
    
    if [ $found_binaries -eq 0 ]; then
        echo -e "${RED}✗ No binaries found after build${NC}"
        return 1
    fi
    
    echo -e "${GREEN}✓ Installation complete${NC}\n"
}

# Verify installation
verify_installation() {
    echo -e "${BLUE}=== Verification ===${NC}\n"
    
    # Add to PATH temporarily
    export PATH="$LOCAL_BIN:$PATH"
    
    local found=0
    
    for binary in llama-cli llama-server llama; do
        if command -v "$binary" &> /dev/null; then
            echo -e "${GREEN}✓ $binary${NC}"
            "$binary" --version 2>&1 | head -1
            found=1
        fi
    done
    
    if [ $found -eq 0 ]; then
        echo -e "${RED}✗ No llama binaries found in PATH${NC}"
        echo -e "\nDebug info:"
        echo "Contents of $LOCAL_BIN:"
        ls -lh "$LOCAL_BIN" 2>/dev/null || echo "Directory not found"
        return 1
    fi
    
    echo ""
}

# Setup persistent PATH
setup_persistent_path() {
    echo -e "${BLUE}Setting up persistent PATH...${NC}\n"
    
    local shells=(".bashrc" ".zshrc" ".bash_profile")
    
    for shell_rc in "${shells[@]}"; do
        local rc_file="$HOME/$shell_rc"
        if [ -f "$rc_file" ]; then
            if ! grep -q "/.local/bin" "$rc_file"; then
                echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$rc_file"
                echo -e "${GREEN}✓ Updated $shell_rc${NC}"
            else
                echo -e "${YELLOW}⚠ $shell_rc already configured${NC}"
            fi
        fi
    done
    
    echo ""
}

# Main execution
main() {
    echo -e "${BLUE}=== llama.cpp Installation & Build ===${NC}\n"
    
    check_prerequisites
    stop_llama_processes
    setup_repository
    build_llama
    install_binaries
    verify_installation
    setup_persistent_path
    
    # Final summary
    echo -e "${GREEN}=== ✓✓✓ Installation Successful! ✓✓✓ ===${NC}\n"
    echo "llama.cpp has been built and installed to: $LOCAL_BIN"
    echo ""
    echo "Available commands:"
    export PATH="$LOCAL_BIN:$PATH"
    command -v llama-cli &>/dev/null && echo "  • llama-cli"
    command -v llama-server &>/dev/null && echo "  • llama-server"
    command -v llama &>/dev/null && echo "  • llama"
    echo ""
    echo "To apply PATH changes to your current shell:"
    echo "  source ~/.bashrc  (or .zshrc, .bash_profile)"
    echo ""
    echo "Or close and reopen your terminal."
}

# Run main function
main "$@"

