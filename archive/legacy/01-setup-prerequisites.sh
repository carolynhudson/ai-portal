#!/bin/bash
# File: 01-setup-prerequisites.sh
# Purpose: Install missing system dependencies and development packages
# Run as: ./01-setup-prerequisites.sh

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[✓]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

log_info "Starting prerequisite installation..."

# Update package manager
log_info "Updating package manager..."
sudo apt update
sudo apt upgrade -y

# Install Python dependencies
log_info "Installing Python development packages..."
sudo apt install -y python3-dev python3-venv python3-pip

# Install numerical libraries
log_info "Installing numerical computation libraries..."
sudo apt install -y libopenblas-dev liblapack-dev libblas-dev gfortran

# Install additional build tools
log_info "Installing additional build dependencies..."
sudo apt install -y build-essential autoconf automake libtool curl wget

# Install compression utilities
sudo apt install -y zip unzip

# Verify Python installation
log_info "Verifying Python setup..."
python3 --version
python3 -m venv --help > /dev/null && log_success "Python venv available" || log_error "venv not available"

# Verify numerical libraries
if pkg-config --exists openblas; then
    log_success "OpenBLAS detected"
else
    log_warn "OpenBLAS package-config not found, but package is installed"
fi

# Create necessary directories
log_info "Creating directory structure..."
mkdir -p "${HOME}/.local/bin"
mkdir -p "${HOME}/.local/share"
mkdir -p "${HOME}/models"
mkdir -p "${HOME}/ai-projects"

# Add ~/.local/bin to PATH if not already present
if ! grep -q '\.local/bin' "${HOME}/.bashrc"; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "${HOME}/.bashrc"
    log_success "Added ~/.local/bin to PATH"
fi

source "${HOME}/.bashrc"

log_success "All prerequisites installed successfully!"
log_info "Next step: Run 02-install-llama-cpp.sh"
