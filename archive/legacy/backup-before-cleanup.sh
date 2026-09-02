#!/bin/bash
set -e

if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root."
   exit 1
fi

BACKUP_DIR="/root/boot-cleanup-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"

echo "=== BOOT CLEANUP SAFETY BACKUP ==="
echo "Backup location: $BACKUP_DIR"
echo ""

# Backup ESP directories that will be deleted
echo "Backing up ESP directories..."
sudo cp -r /boot/efi/EFI/bazzite "$BACKUP_DIR/bazzite-efi" 2>/dev/null && echo "✓ Backed up /boot/efi/EFI/bazzite" || echo "⚠ /boot/efi/EFI/bazzite not found"
sudo cp -r /boot/efi/bazzite-kernel "$BACKUP_DIR/bazzite-kernel" 2>/dev/null && echo "✓ Backed up /boot/efi/bazzite-kernel" || echo "⚠ /boot/efi/bazzite-kernel not found"
sudo cp -r /boot/efi/EFI/systemd "$BACKUP_DIR/systemd-efi" 2>/dev/null && echo "✓ Backed up /boot/efi/EFI/systemd" || echo "⚠ /boot/efi/EFI/systemd not found"
sudo cp -r /boot/efi/loader "$BACKUP_DIR/loader" 2>/dev/null && echo "✓ Backed up /boot/efi/loader" || echo "⚠ /boot/efi/loader not found"

# Backup old kernel files
echo ""
echo "Backing up old kernel files from /boot..."
sudo cp /boot/initrd.img-7.0.0-29-generic "$BACKUP_DIR/initrd.img-7.0.0-29-generic" 2>/dev/null && echo "✓ Backed up old initrd" || echo "⚠ Old initrd not found"
sudo cp /boot/vmlinuz-7.0.0-29-generic "$BACKUP_DIR/vmlinuz-7.0.0-29-generic" 2>/dev/null && echo "✓ Backed up old vmlinuz" || echo "⚠ Old vmlinuz not found"

# Backup UEFI firmware state
echo ""
echo "Backing up UEFI boot entries..."
sudo efibootmgr -v > "$BACKUP_DIR/uefi-boot-entries-before.txt" 2>/dev/null && echo "✓ Backed up UEFI boot entries" || echo "⚠ Could not backup UEFI entries"

# Backup current grub and systemd-boot configs
echo ""
echo "Backing up configuration files..."
sudo cp /etc/default/grub "$BACKUP_DIR/grub-default" 2>/dev/null && echo "✓ Backed up /etc/default/grub" || echo "⚠ /etc/default/grub not found"
sudo cp -r /etc/grub.d "$BACKUP_DIR/grub-d" 2>/dev/null && echo "✓ Backed up /etc/grub.d" || echo "⚠ /etc/grub.d not found"

# Tar the backup for safety
echo ""
echo "Compressing backup..."
tar -czf "$BACKUP_DIR.tar.gz" -C "$(dirname "$BACKUP_DIR")" "$(basename "$BACKUP_DIR")" && echo "✓ Compressed backup to $BACKUP_DIR.tar.gz"
echo ""
echo "=== BACKUP COMPLETE ==="
echo "Original backup dir: $BACKUP_DIR"
echo "Compressed backup: $BACKUP_DIR.tar.gz"
echo ""
echo "If cleanup goes wrong, you can restore from: $BACKUP_DIR/"
