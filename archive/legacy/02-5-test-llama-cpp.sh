#!/bin/bash
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}=== llama.cpp Validation & Testing ===${NC}"

# Check if llama.cpp is installed
if ! command -v llama-cli &> /dev/null; then
    echo -e "${RED}✗ llama.cpp not found in PATH${NC}"
    exit 1
fi

echo -e "${GREEN}✓ llama.cpp found${NC}"
llama-cli --version

# Test ROCm detection
echo -e "\n${BLUE}Testing ROCm GPU detection...${NC}"
if llama-cli --help 2>&1 | grep -q "ROCM"; then
    echo -e "${GREEN}✓ ROCm support compiled in${NC}"
else
    echo -e "${YELLOW}⚠ ROCm support not detected in help output${NC}"
fi

# Test server startup
echo -e "\n${BLUE}Testing llama-server startup...${NC}"
if ! command -v llama-server &> /dev/null; then
    echo -e "${RED}✗ llama-server not found${NC}"
    exit 1
fi

echo -e "${GREEN}✓ llama-server available${NC}"

# Check for GGUF models
MODELS_DIR="$HOME/models"
if [ -d "$MODELS_DIR" ]; then
    GGUF_COUNT=$(find "$MODELS_DIR" -name "*.gguf" 2>/dev/null | wc -l)
    echo -e "\n${BLUE}Found $GGUF_COUNT GGUF models in $MODELS_DIR${NC}"
    if [ $GGUF_COUNT -gt 0 ]; then
        echo -e "${GREEN}✓ Models available for testing${NC}"
        find "$MODELS_DIR" -name "*.gguf" -type f | head -3 | while read model; do
            SIZE=$(du -h "$model" | cut -f1)
            echo -e "  - $(basename "$model") ($SIZE)"
        done
    fi
else
    echo -e "${YELLOW}⚠ Models directory not found at $MODELS_DIR${NC}"
fi

# Test GPU memory access
echo -e "\n${BLUE}Testing GPU memory access...${NC}"
if rocm-smi &> /dev/null; then
    echo -e "${GREEN}✓ ROCm GPU accessible${NC}"
    rocm-smi --showmeminfo
else
    echo -e "${YELLOW}⚠ rocm-smi not available${NC}"
fi

# Recommend next steps
echo -e "\n${BLUE}=== Next Steps ===${NC}"
echo "1. Run a quick inference test with a small model"
echo "2. Start llama-server with:"
echo "   llama-server -m ~/models/your-model.gguf -gpu 0 --ctx-size 2048"
echo "3. Test API endpoint: curl http://localhost:8000/health"

echo -e "\n${GREEN}✓ llama.cpp validation complete!${NC}"
