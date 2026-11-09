#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
APP="mac/build/XM Control.app"
# Recreate bundled dashboard assets so removed files do not linger in the app.
rm -rf "$APP/Contents/Resources/dashboard"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/dashboard" mac/.module-cache
cp dashboard/index.html dashboard/style.css dashboard/alerts.css dashboard/app.js "$APP/Contents/Resources/dashboard/"
.venv/bin/python -c 'import json,pathlib; pathlib.Path("mac/build/XM Control.app/Contents/Resources/project.json").write_text(json.dumps({"root":str(pathlib.Path.cwd())}))'
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>XMControl</string>
<key>CFBundleIdentifier</key><string>local.xm.control</string>
<key>CFBundleName</key><string>XM Control</string>
<key>CFBundleDisplayName</key><string>XM Control</string>
<key>CFBundleVersion</key><string>1</string>
<key>CFBundleShortVersionString</key><string>1.0</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleIconFile</key><string>AppIcon</string>
<key>LSMinimumSystemVersion</key><string>13.0</string>
<key>NSHighResolutionCapable</key><true/>
<key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST
xcrun swiftc -module-cache-path mac/.module-cache mac/MakeIcon.swift -o mac/build/make-icon
mac/build/make-icon mac/build/AppIcon.iconset
iconutil -c icns mac/build/AppIcon.iconset -o "$APP/Contents/Resources/AppIcon.icns"
xcrun swiftc -swift-version 5 -O -module-cache-path mac/.module-cache -framework AppKit -framework WebKit -framework UserNotifications mac/XMControl.swift -o "$APP/Contents/MacOS/XMControl"
codesign --force --sign - --identifier local.xm.control "$APP"
printf 'Built: %s/%s\n' "$PWD" "$APP"
