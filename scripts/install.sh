#!/bin/sh

INSTALL_DIR="/mnt/us/kindle_hid_passthrough"

# Directory holding this script, so sibling scripts resolve regardless of cwd.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# True when the script is running from the install dir itself, so the
# cp commands below would be a no-op (source == destination).
in_install_dir()
{
  [ "$(cd "$(dirname "$0")/.." && pwd)" = "$INSTALL_DIR" ] || \
    [ "$(pwd)" = "$INSTALL_DIR" ]
}

installMainFiles()
{
  echo " -> Installing main program files"
  mkdir -p "$INSTALL_DIR/dist" "$INSTALL_DIR/illusion/BTManager" "$INSTALL_DIR/cache"
  if ! in_install_dir; then
    cp -r dist/* "$INSTALL_DIR/dist/"
    cp kindle-hid-passthrough "$INSTALL_DIR/"
    cp libsyscall_wrapper.so "$INSTALL_DIR/"
    cp config.ini "$INSTALL_DIR/"
  fi
  chmod +x "$INSTALL_DIR/kindle-hid-passthrough"
  echo " -> Ready."
}

installAll()
{
  echo ""
  echo "=== Full Install ==="
  installUdevRules
  installUpstart
  installMainFiles
  installWAFApp
  if [ -d /mnt/us/koreader/plugins/ ]; then
    installKOReaderPlugin
  fi
  echo ""
  echo "Installation complete. Open 'BT Manager' from the Kindle library."
}

installUdevRules()
{
  echo " -> Installing udev rules"
  /usr/sbin/mntroot rw
  cp assets/99-hid-keyboard.rules /etc/udev/rules.d
  /usr/sbin/udevadm control --reload-rules
  /usr/sbin/mntroot ro
  echo " -> Ready."
}

installUpstart()
{
  echo " -> Installing upstart service"
  /usr/sbin/mntroot rw
  cp assets/hid-passthrough.upstart /etc/upstart/hid-passthrough.conf
  /usr/sbin/mntroot ro
  echo " -> Ready."
}

pairDevice()
{
  ./kindle-hid-passthrough --pair 2>&1 | grep -v "libenvload.so"
}

listDevices()
{
  cat devices.conf
}

setLayout()
{
  printf "Enter layout code (e.g. fr, de, 'fr(oss)'): "
  read layout
  /bin/sh "$SCRIPT_DIR/setlayout.sh" "$layout"
}

installWAFApp()
{
  echo " -> Installing BTManager app"
  if ! in_install_dir; then
    cp -r illusion/BTManager/* "$INSTALL_DIR/illusion/BTManager/"
    cp illusion/BTManager.sh "$INSTALL_DIR/illusion/BTManager.sh"
    cp illusion/install-waf-app.sh "$INSTALL_DIR/illusion/install-waf-app.sh"
  fi
  if [ -f "$INSTALL_DIR/illusion/install-waf-app.sh" ]; then
    /bin/sh "$INSTALL_DIR/illusion/install-waf-app.sh"
  else
    echo "ERROR: $INSTALL_DIR/illusion/install-waf-app.sh not found"
  fi
}

installKOReaderPlugin()
{
  if [ ! -d /mnt/us/koreader/plugins/ ]; then
    echo " -> KOReader not found, skipping plugin install"
    return
  fi
  echo " -> Installing KOReader plugin"
  cp -r koreader-plugin/hidpassthrough.koplugin /mnt/us/koreader/plugins/hidpassthrough.koplugin
  echo " -> Ready."
}

uninstallAll()
{
  echo ""
  echo "=== Uninstall ==="
  printf "This will stop the daemon, remove udev/upstart/WAF app, and delete the install directory.\n"
  printf "Continue? [y/N]: "
  read confirm
  case "$confirm" in
    y|Y|yes|YES) ;;
    *) echo "Aborted."; return ;;
  esac

  APP_ID="com.lzampier.btmanager"
  INSTALL_DIR="/mnt/us/kindle_hid_passthrough"
  SCRIPTLET_DEST="/mnt/us/documents/BTManager.sh"
  APPREG_DB="/var/local/appreg.db"

  echo " -> Stopping daemon"
  /sbin/stop hid-passthrough 2>/dev/null
  pkill -f "kindle-hid-passthrough" 2>/dev/null
  pkill -f "main.py --daemon" 2>/dev/null
  pkill -f "ld-linux-armhf." 2>/dev/null

  /usr/sbin/mntroot rw

  echo " -> Removing upstart config"
  rm -f /etc/upstart/hid-passthrough.conf

  echo " -> Removing udev rules"
  rm -f /etc/udev/rules.d/99-hid-keyboard.rules
  if [ -f /usr/local/bin/dev_is_keyboard.sh ]; then
    rm -f /usr/local/bin/dev_is_keyboard.sh
  fi
  /usr/sbin/udevadm control --reload-rules 2>/dev/null

  echo " -> Unregistering WAF app"
  if [ -f "$APPREG_DB" ]; then
    sqlite3 "$APPREG_DB" <<EOF 2>/dev/null
DELETE FROM properties WHERE handlerId='$APP_ID';
DELETE FROM associations WHERE handlerId='$APP_ID';
DELETE FROM handlerIds WHERE handlerId='$APP_ID';
EOF
  fi
  rm -f "$SCRIPTLET_DEST"

  /usr/sbin/mntroot ro

  echo " -> Removing install directory $INSTALL_DIR"
  cd /tmp
  rm -rf "$INSTALL_DIR"

  echo ""
  echo "Uninstall complete. Reboot recommended."
}

print_menu()
{
  printf "\nSelect an option:\n"
  printf " 1) Install everything (recommended)\n"
  printf " 2) Pair Bluetooth keyboard\n"
  printf " 3) List paired devices\n"
  printf " 4) Install udev rules (keyboard service)\n"
  printf " 5) Install upstart (auto-start on boot)\n"
  printf " 6) Install BTManager app\n"
  printf " 7) Set custom keyboard layout\n"
  printf " 8) Install KOReader plugin\n"
  printf " 9) Uninstall everything\n"
  printf "10) Quit\n"
}

# Non-interactive entry point: `sh install.sh <action>` runs one action and exits.
if [ $# -gt 0 ]; then
  case "$1" in
    installAll)         installAll; exit 0 ;;
    installUdevRules)   installUdevRules; exit 0 ;;
    installUpstart)     installUpstart; exit 0 ;;
    installMainFiles)   installMainFiles; exit 0 ;;
    installWAFApp)      installWAFApp; exit 0 ;;
    installKOReaderPlugin) installKOReaderPlugin; exit 0 ;;
    uninstallAll)       uninstallAll; exit 0 ;;
    *) echo "Unknown action: $1" >&2; exit 1 ;;
  esac
fi

while :; do
  print_menu
  printf "Enter choice [1-10]: "
  read choice
  case "$choice" in
    1)
      installAll
      ;;
    2)
      pairDevice
      ;;
    3)
      listDevices
      ;;
    4)
      installUdevRules
      ;;
    5)
      installUpstart
      ;;
    6)
      installWAFApp
      ;;
    7)
      setLayout
      ;;
    8)
      installKOReaderPlugin
      ;;
    9)
      uninstallAll
      ;;
    10)
      echo "Exiting."
      break
      ;;
    *)
      printf "Invalid option: %s\n" "$choice"
      ;;
  esac
done
