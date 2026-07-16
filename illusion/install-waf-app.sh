#!/bin/sh
#
# install-waf-app.sh - Install BTManager WAF app (Illusion)
# Run this on the Kindle to register and set up the BTManager WAF app
#

APP_ID="com.lzampier.btmanager"
APP_DIR="/mnt/us/kindle_hid_passthrough/illusion/BTManager"
ILLUSION_DIR="/mnt/us/kindle_hid_passthrough/illusion"
SOURCE_DIR="$APP_DIR"
BINARY="$ILLUSION_DIR/../kindle-hid-passthrough"
SCRIPTLET="$ILLUSION_DIR/BTManager.sh"
SCRIPTLET_DEST="/mnt/us/documents/BTManager.sh"
APPREG_DB="/var/local/appreg.db"

echo ""
echo "=== BTManager Installer ==="
echo ""

# ---- Check prerequisites ----

if [ ! -f "$SOURCE_DIR/config.xml" ]; then
    echo "ERROR: App files not found at $SOURCE_DIR"
    echo "Make sure kindle_hid_passthrough is installed first."
    exit 1
fi

if [ ! -f "$BINARY" ]; then
    echo "ERROR: kindle-hid-passthrough binary not found at $BINARY"
    exit 1
fi

# ---- Install WAF app ----

echo "1. Checking WAF app files..."
if [ ! -d "$APP_DIR" ]; then
    echo "   ERROR: App directory not found at $APP_DIR"
    echo "   Deploy kindle_hid_passthrough first."
    exit 1
fi
echo "   Files at $APP_DIR"

# ---- Make helper executable ----

echo "2. Setting permissions..."
chmod +x "$SCRIPTLET"
echo "   Done"

# ---- Purge stale WAF cache ----
#
# mesquite caches the parsed chrome config and rendered assets per-app under
# /var/local/mesquite. That cache survives an uninstall/reinstall, so an old
# entry keeps the previous chrome (e.g. a hidden status bar) even after the
# app files are updated. Purge it on every install so config.xml changes take.
echo "3. Purging stale WAF cache..."
MESQUITE_DIR="/var/local/mesquite"
# Unload any running instance via appmgrd so the next launch re-reads config.xml.
# Not pkill: killing mesquite makes appmgrd report a crash to the user.
lipc-set-prop com.lab126.appmgrd stop "app://$APP_ID" 2>/dev/null
sleep 1
if [ -d "$MESQUITE_DIR" ]; then
    for d in "$MESQUITE_DIR"/*[Bb][Tt][Mm]anager* "$MESQUITE_DIR/BT Manager" "$MESQUITE_DIR/$APP_ID"; do
        [ -e "$d" ] || continue
        rm -rf "$d"
        echo "   Removed $d"
    done
fi
echo "   Done"

# ---- Register in appreg.db ----

echo "4. Registering app..."
if [ -f "$APPREG_DB" ]; then
    # Rewritten every time, an existing registration can still be pointing at a
    # stale command or app path after an update.
    sqlite3 "$APPREG_DB" <<EOF
INSERT OR IGNORE INTO interfaces (interface) VALUES ('application');
INSERT OR IGNORE INTO handlerIds (handlerId) VALUES ('$APP_ID');
INSERT OR IGNORE INTO associations (handlerId, interface, contentId, defaultAssoc)
    VALUES ('$APP_ID', 'application', 'GL:$APP_ID', 0);
INSERT OR REPLACE INTO properties (handlerId, name, value)
    VALUES ('$APP_ID', 'lipcId', '$APP_ID');
INSERT OR REPLACE INTO properties (handlerId, name, value)
    VALUES ('$APP_ID', 'command', '/usr/bin/mesquite -l $APP_ID -c file://$APP_DIR/');
INSERT OR REPLACE INTO properties (handlerId, name, value)
    VALUES ('$APP_ID', 'supportedOrientation', 'U');
EOF
    echo "   Registered $APP_ID in appreg.db"
else
    echo "   WARNING: appreg.db not found at $APPREG_DB"
fi

# ---- Install scriptlet ----

echo "5. Installing scriptlet..."
cp "$SCRIPTLET" "$SCRIPTLET_DEST"
chmod +x "$SCRIPTLET_DEST"
echo "   Installed to $SCRIPTLET_DEST"

# ---- Start helper ----

echo "6. Starting daemon..."
# Stop existing instances
/sbin/stop hid-passthrough 2>/dev/null
pkill -f "ld-linux-armhf." 2>/dev/null
sleep 1
if [ -f /etc/upstart/hid-passthrough.conf ] && \
   /sbin/start hid-passthrough >/dev/null 2>&1; then
    echo "   Daemon started via upstart"
else
    "$BINARY" --daemon >/dev/null 2>&1 &
    sleep 1
    echo "   Daemon started"
fi

echo ""
echo "=== Installation Complete ==="
echo ""
echo "You can now:"
echo "  - Open 'BT Manager' from the Kindle library (scriptlet)"
echo "  - Or launch directly: lipc-set-prop com.lab126.appmgrd start app://$APP_ID"
echo ""
echo "To start on boot, install the upstart config:"
echo "  cp $ILLUSION_DIR/../assets/hid-passthrough.upstart /etc/upstart/hid-passthrough.conf"
echo ""
