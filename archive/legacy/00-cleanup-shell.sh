#!/bin/bash

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}=== Shell Configuration Cleanup ===${NC}\n"

# Backup shell config files
for RC_FILE in ~/.bashrc ~/.zshrc; do
    if [ -f "$RC_FILE" ]; then
        BACKUP="${RC_FILE}.backup.$(date +%s)"
        cp "$RC_FILE" "$BACKUP"
        echo -e "${GREEN}✓ Backed up $RC_FILE to $BACKUP${NC}"
    fi
done

# Remove duplicate PATH exports
echo -e "\n${BLUE}Cleaning up PATH exports...${NC}"

# Create clean bashrc (removing duplicates and problematic entries)
if [ -f ~/.bashrc ]; then
    grep -v "HOME/.local/bin" ~/.bashrc | grep -v "llama" > ~/.bashrc.tmp
    mv ~/.bashrc.tmp ~/.bashrc
    echo -e "${GREEN}✓ Cleaned ~/.bashrc${NC}"
fi

if [ -f ~/.zshrc ]; then
    grep -v "HOME/.local/bin" ~/.zshrc | grep -v "llama" > ~/.zshrc.tmp
    mv ~/.zshrc.tmp ~/.zshrc
    echo -e "${GREEN}✓ Cleaned ~/.zshrc${NC}"
fi

# Add PATH export ONCE to bashrc
if [ -f ~/.bashrc ]; then
    echo "" >> ~/.bashrc
    echo "# Added by llama.cpp setup" >> ~/.bashrc
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
    echo -e "${GREEN}✓ Added clean PATH export to ~/.bashrc${NC}"
fi

echo -e "\n${BLUE}To apply changes, restart your shell or run:${NC}"
echo "  bash --norc"
echo ""
echo -e "${GREEN}✓ Cleanup complete!${NC}"
