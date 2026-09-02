#!/bin/bash
set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Configuration
SOURCE_DIR="${1:-$HOME/models}"
DEST_DIR="${2:-$HOME/.cache/llama.cpp/models}"
LOG_FILE="$HOME/model_migration_$(date +%Y%m%d_%H%M%S).log"

echo -e "${BLUE}=== LM Studio Model Migration to llama.cpp ===${NC}"
echo "Source: $SOURCE_DIR"
echo "Destination: $DEST_DIR"
echo "Log: $LOG_FILE"

# Validation
if [ ! -d "$SOURCE_DIR" ]; then
    echo -e "${RED}✗ Source directory not found: $SOURCE_DIR${NC}"
    exit 1
fi

# Create destination
mkdir -p "$DEST_DIR"

# Initialize counters
TOTAL_MODELS=0
MIGRATED_MODELS=0
SKIPPED_MODELS=0
FAILED_MODELS=0

# Find all GGUF models in LM Studio directory structure
echo -e "\n${BLUE}Scanning for GGUF models...${NC}"

while IFS= read -r model_file; do
    TOTAL_MODELS=$((TOTAL_MODELS + 1))
    REL_PATH="${model_file#$SOURCE_DIR/}"
    DEST_FILE="$DEST_DIR/$(basename "$model_file")"
    SIZE=$(du -h "$model_file" | cut -f1)
    
    # Check if already exists
    if [ -f "$DEST_FILE" ] && cmp -s "$model_file" "$DEST_FILE"; then
        echo -e "${YELLOW}⊘ Already migrated: $REL_PATH ($SIZE)${NC}" | tee -a "$LOG_FILE"
        SKIPPED_MODELS=$((SKIPPED_MODELS + 1))
        continue
    fi
    
    # Check destination space
    REQUIRED_SPACE=$(du -b "$model_file" | cut -f1)
    AVAILABLE_SPACE=$(df "$DEST_DIR" | tail -1 | awk '{print $4 * 1024}')
    
    if [ "$AVAILABLE_SPACE" -lt "$REQUIRED_SPACE" ]; then
        echo -e "${RED}✗ Insufficient space for $REL_PATH (need $SIZE, have $(numfmt --to=iec $AVAILABLE_SPACE 2>/dev/null || echo 'unknown'))${NC}" | tee -a "$LOG_FILE"
        FAILED_MODELS=$((FAILED_MODELS + 1))
        continue
    fi
    
    # Copy with progress
    echo -e "${BLUE}→ Migrating: $REL_PATH ($SIZE)${NC}" | tee -a "$LOG_FILE"
    
    if cp -v "$model_file" "$DEST_FILE" 2>&1 | tee -a "$LOG_FILE"; then
        # Verify integrity
        SOURCE_MD5=$(md5sum "$model_file" | cut -d' ' -f1)
        DEST_MD5=$(md5sum "$DEST_FILE" | cut -d' ' -f1)
        
        if [ "$SOURCE_MD5" = "$DEST_MD5" ]; then
            echo -e "${GREEN}✓ Successfully migrated: $(basename "$model_file")${NC}" | tee -a "$LOG_FILE"
            MIGRATED_MODELS=$((MIGRATED_MODELS + 1))
        else
            echo -e "${RED}✗ Checksum mismatch for $REL_PATH${NC}" | tee -a "$LOG_FILE"
            rm -f "$DEST_FILE"
            FAILED_MODELS=$((FAILED_MODELS + 1))
        fi
    else
        echo -e "${RED}✗ Failed to copy: $REL_PATH${NC}" | tee -a "$LOG_FILE"
        FAILED_MODELS=$((FAILED_MODELS + 1))
    fi
    
done < <(find "$SOURCE_DIR" -name "*.gguf" -type f 2>/dev/null)

# Create model index for llama.cpp
INDEX_FILE="$HOME/.cache/llama.cpp/models/INDEX.json"
echo -e "\n${BLUE}Creating model index...${NC}"

python3 << 'PYTHON_EOF'
import os
import json
from pathlib import Path

models_dir = Path(os.path.expanduser("~/.cache/llama.cpp/models"))
index = {"models": [], "metadata": {"created": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"}}

for model_file in sorted(models_dir.glob("*.gguf")):
    size_mb = model_file.stat().st_size / (1024 * 1024)
    index["models"].append({
        "name": model_file.name,
        "path": str(model_file),
        "size_mb": round(size_mb, 2)
    })

with open(models_dir / "INDEX.json", "w") as f:
    json.dump(index, f, indent=2)

print(f"Created index with {len(index['models'])} models")
PYTHON_EOF

# Summary
echo -e "\n${BLUE}=== Migration Summary ===${NC}"
echo "Total models found: $TOTAL_MODELS"
echo -e "${GREEN}Successfully migrated: $MIGRATED_MODELS${NC}"
echo -e "${YELLOW}Skipped (already migrated): $SKIPPED_MODELS${NC}"
echo -e "${RED}Failed: $FAILED_MODELS${NC}"

if [ "$FAILED_MODELS" -eq 0 ] && [ "$MIGRATED_MODELS" -gt 0 ]; then
    echo -e "\n${GREEN}✓ Migration complete!${NC}"
    echo "Models are ready at: $DEST_DIR"
else
    echo -e "\n${YELLOW}⚠ Migration completed with warnings.${NC}"
fi

echo "Full log: $LOG_FILE"
