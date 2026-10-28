#!/bin/bash
# Builds "Journal Watcher.app", the zip the in-app updater downloads, and the disk image people drag to Applications.
#   app/build_app.sh            build, sign, self-test, zip, disk image
#   app/build_app.sh --quick    just the app (and its self-test)
# Needs: macOS, Xcode command line tools, the project's Python (the .venv, or python3 on the PATH). Output: app/dist/
# Environment (all optional): JW_SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" signs for real (hardened runtime);
#   JW_NOTARY_PROFILE=<a keychain profile made with `xcrun notarytool store-credentials`> also notarises and staples (see docs/HISTORY.md §2l).
#   JW_SKIP_SELFTEST=1 skips opening a window (for machines without a screen session); JW_RELEASE=1 never bakes in a personal folder; JW_SUGGEST_HOME=/path offers it on first start.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
APP="$ROOT/app"
DIST="$APP/dist"
BUILD="$APP/build"
NAME="Journal Watcher"
VERSION="$(tr -d '[:space:]' < "$APP/VERSION")"
QUICK=0; [ "${1:-}" = "--quick" ] && QUICK=1

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

say "1/7 build environment (app/.buildenv), version $VERSION"
if [ ! -x "$APP/.buildenv/bin/python" ]; then
  BASEPY="python3"
  if [ -x "$ROOT/.venv/bin/python" ]; then BASEPY="$("$ROOT/.venv/bin/python" -c 'import sys; print(sys.base_prefix + "/bin/python3")')"; fi
  "$BASEPY" -m venv "$APP/.buildenv"
fi
PY="$APP/.buildenv/bin/python"
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -r "$ROOT/requirements.txt" pywebview pyinstaller dmgbuild

say "2/7 icon, disk-image picture and the files a new data folder starts from"
rm -rf "$BUILD"; mkdir -p "$BUILD/defaults"
"$PY" "$APP/make_icon.py" "$BUILD" >/dev/null
"$PY" "$APP/make_dmg_art.py" "$BUILD" >/dev/null
for f in ai.json journals.json interests.json library.json prompts.json; do cp "$ROOT/.agents/skills/ais-journal-watcher/defaults/$f" "$BUILD/defaults/$f"; done
# a folder offered on the first start as "my existing folder": only for builds for yourself (JW_SUGGEST_HOME=/path, or a line in app/suggested_home.txt, which is git-ignored); never in a release
SUGGEST="${JW_SUGGEST_HOME:-}"; [ -z "$SUGGEST" ] && [ -s "$APP/suggested_home.txt" ] && SUGGEST="$(head -1 "$APP/suggested_home.txt")"
if [ -n "$SUGGEST" ] && [ -z "${JW_RELEASE:-}" ]; then printf '%s\n' "$SUGGEST" > "$BUILD/defaults/suggested_home.txt"; echo "first start will offer: $SUGGEST"; fi
printf '{"version": "%s"}\n' "$VERSION" > "$BUILD/defaults/version.json"
if [ -s "$APP/update_feed.txt" ]; then tr -d '[:space:]' < "$APP/update_feed.txt" > "$BUILD/defaults/update_feed.txt"; echo "update feed: $(cat "$BUILD/defaults/update_feed.txt")"; else echo "no app/update_feed.txt: this build will not look for updates until you set an address in Settings"; fi

say "3/7 PyInstaller"
rm -rf "$DIST" 2>/dev/null || { sleep 2; rm -rf "$DIST"; }; mkdir -p "$DIST"       # (Finder may be writing a .DS_Store into it)
"$PY" -m PyInstaller --noconfirm --clean --distpath "$DIST" --workpath "$BUILD/work" "$APP/JournalWatcher.spec" 2>&1 | grep -E "ERROR|WARNING: (Hidden|lib)|Building BUNDLE|completed successfully" || true
[ -d "$DIST/$NAME.app" ] || { echo "The build failed: see the lines above."; exit 1; }
rm -rf "$DIST/$NAME"                      # PyInstaller also leaves the unbundled folder next to the .app (a second copy of everything)

say "4/7 signing"
xattr -cr "$DIST/$NAME.app"
if [ -n "${JW_SIGN_IDENTITY:-}" ]; then
  codesign --force --deep --options runtime --timestamp --entitlements "$APP/entitlements.plist" --sign "$JW_SIGN_IDENTITY" "$DIST/$NAME.app"
  echo "signed with: $JW_SIGN_IDENTITY"
else
  codesign --force --deep --sign - "$DIST/$NAME.app" 2>&1 | tail -2
  echo "signed ad hoc (no Developer ID): opens on this Mac without a prompt; see HANDOFF §2l for other Macs"
fi
codesign --verify --deep --strict "$DIST/$NAME.app" && echo "signature ok"
if [ -n "${JW_NOTARY_PROFILE:-}" ] && [ -n "${JW_SIGN_IDENTITY:-}" ]; then
  say "notarising (Apple checks the app; a few minutes)"
  ditto -c -k --keepParent "$DIST/$NAME.app" "$BUILD/notarize.zip"
  xcrun notarytool submit "$BUILD/notarize.zip" --keychain-profile "$JW_NOTARY_PROFILE" --wait
  xcrun stapler staple "$DIST/$NAME.app"
fi

if [ -n "${JW_SKIP_SELFTEST:-}" ]; then say "5/7 self-test skipped (JW_SKIP_SELFTEST)"; else
say "5/7 self-test: the finished app opens a window on a scratch data folder"
T="$(mktemp -d)"
JW_NO_UPDATE_CHECK=1 JW_UPDATE_DIR="$T/updates" JW_HOME="$T/home" JW_APP_TEST="$T/result.json" JW_APP_TEST_SAVE="$T/saved.bin" "$DIST/$NAME.app/Contents/MacOS/$NAME" > "$T/app.log" 2>&1 &
APID=$!
for i in $(seq 1 180); do [ -f "$T/result.json" ] && break; kill -0 $APID 2>/dev/null || break; sleep 1; done
kill $APID 2>/dev/null || true
if [ -f "$T/result.json" ]; then cat "$T/result.json"; else echo "no result; the log:"; tail -40 "$T/app.log"; exit 1; fi
rm -rf "$T"
fi

if [ "$QUICK" = 1 ]; then say "done (no zip, no disk image): $DIST/$NAME.app"; exit 0; fi

say "6/7 the zip for the in-app updater"
ZIP="$DIST/Journal-Watcher-$VERSION.zip"
ditto -c -k --keepParent "$DIST/$NAME.app" "$ZIP"
ls -lh "$ZIP"

say "7/7 disk image"
rm -f "$DIST/$NAME.dmg"
"$APP/.buildenv/bin/dmgbuild" -s "$APP/dmg_settings.py" -D app="$DIST/$NAME.app" -D bg="$BUILD/dmg-bg.tiff" -D icon="$BUILD/JournalWatcher.icns" "$NAME" "$DIST/$NAME.dmg" >/dev/null
if [ -n "${JW_SIGN_IDENTITY:-}" ]; then codesign --force --timestamp --sign "$JW_SIGN_IDENTITY" "$DIST/$NAME.dmg"; fi
if [ -n "${JW_NOTARY_PROFILE:-}" ] && [ -n "${JW_SIGN_IDENTITY:-}" ]; then xcrun notarytool submit "$DIST/$NAME.dmg" --keychain-profile "$JW_NOTARY_PROFILE" --wait && xcrun stapler staple "$DIST/$NAME.dmg"; fi
ls -lh "$DIST/$NAME.dmg"
say "done: $DIST/$NAME.dmg and $ZIP"
