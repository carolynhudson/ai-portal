#!/bin/bash

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}=== llama.cpp Installation Diagnostic ===${NC}\n"

# Check common installation locations
echo -e "${BLUE}Checking for llama.cpp installations...${NC}"

LOCATIONS=(
    "$HOME/llama.cpp"
    "$HOME/ai-tools/llama.cpp"
    "/opt/llama.cpp"
    "/usr/local/llama.cpp"
)

FOUND_LOCATIONS=()

for loc in "${LOCATIONS[@]}"; do
    if [ -d "$loc" ]; then
        echo -e "${GREEN}✓ Found: $loc${NC}"
        FOUND_LOCATIONS+=("$loc")
    fi
done

if [ ${#FOUND_LOCATIONS[@]} -eq 0 ]; then
    echo -e "${RED}✗ No llama.cpp installation found${NC}\n"
    exit 1
fi

echo ""

# Check each location
for LLAMA_HOME in "${FOUND_LOCATIONS[@]}"; do
    echo -e "${BLUE}Analyzing: $LLAMA_HOME${NC}"
    
    # Check build directory
    if [ -d "$LLAMA_HOME/build" ]; then
        echo -e "${GREEN}✓ Build directory exists${NC}"
        
        # List binaries in build
        if ls "$LLAMA_HOME/build"/bin/* &> /dev/null 2>&1; then
            echo "  Binaries in build/bin:"
            ls -lh "$LLAMA_HOME/build"/bin/ 2>/dev/null || echo "    (empty or permission denied)"
        fi
        
        # Check for cmake-built binaries directly in build/
        if ls "$LLAMA_HOME/build"/llama* &> /dev/null 2>&1; then
            echo "  Binaries in build root:"
            ls -lh "$LLAMA_HOME/build"/llama* 2>/dev/null
        fi
    else
        echo -e "${YELLOW}⚠ No build directory${NC}"
    fi
    
    # Check source directory
    if ls "$LLAMA_HOME"/*.cpp &> /dev/null 2>&1; then
        echo -e "${YELLOW}⚠ Source code present but not built${NC}"
    fi
    
    echo ""
done

# Check PATH
echo -e "${BLUE}Current PATH:${NC}"
echo "$PATH" | tr ':' '\n' | head -10

echo ""

# Check for llama binaries anywhere
echo -e "${BLUE}Searching for llama binaries in system...${NC}"
which llama-cli llama-server llama serve llama 2>/dev/null || echo "  (none found in PATH)"

# Check for old LM Studio remnants
echo -e "\n${BLUE}Checking for LM Studio binaries...${NC}"
if [ -d "$HOME/.cache/lm-studio" ]; then
    echo -e "${YELLOW}⚠ LM Studio cache found at ~/.cache/lm-studio${NC}"
    du -sh "$HOME/.cache/lm-studio" 2>/dev/null
fi

echo -e "\n${BLUE}=== Diagnostic Complete ===${NC}"
echo ""
echo "Next steps:"
echo "1. If build directory found, run: source $LLAMA_HOME/build/bin/activate 2>/dev/null || export PATH=\$LLAMA_HOME/build/bin:\$PATH"
echo "2. Verify: llama-cli --version"
echo "3. If still not found, we'll rebuild with proper installation"
