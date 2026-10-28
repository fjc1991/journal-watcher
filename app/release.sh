#!/bin/bash
# Make a new version and tell every installed copy that it exists ("Update to 1.2.0" appears in their top bar).
#
#   app/release.sh --notes "Faster library|New welcome guide" [--bump patch|minor|major|none]
#
# What it does: raises app/VERSION, builds (build_app.sh), writes app/release/appcast.json (the feed the apps ask), and copies the zip, the disk image and the
# feed into the folder named in app/release.conf (PUBLISH_DIR: a Dropbox or iCloud folder, a website folder…). Running apps look at the feed a little after they start
# and every six hours; Help → Check for Updates… looks at once.
# One-time set-up: app/release.conf (see app/release.conf.example) and the feed address in app/update_feed.txt (built into every app).
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"; APP="$ROOT/app"
BUMP="patch"; NOTES=""; NOTES_FILE=""
while [ $# -gt 0 ]; do case "$1" in
  --bump) BUMP="$2"; shift 2;; --notes) NOTES="$2"; shift 2;; --notes-file) NOTES_FILE="$2"; shift 2;;
  *) echo "unknown option $1"; exit 2;; esac; done
[ -n "$NOTES$NOTES_FILE" ] || { echo "Say what is new: --notes \"one|two\" (people see it in the update dialog)."; exit 2; }
[ -f "$APP/release.conf" ] && . "$APP/release.conf"
PUBLISH_DIR="${PUBLISH_DIR:-}"
ZIP_NAME_TEMPLATE="${ZIP_NAME_TEMPLATE:-Journal-Watcher-{version}.zip}"
ZIP_URL_TEMPLATE="${ZIP_URL_TEMPLATE:-}"
[ -n "$ZIP_URL_TEMPLATE" ] || { echo "app/release.conf must set ZIP_URL_TEMPLATE: the address people download the zip from (use {version} if it changes with the version). See app/release.conf.example."; exit 2; }

OLD="$(tr -d '[:space:]' < "$APP/VERSION")"
IFS=. read -r MA MI PA <<< "$OLD"; PA="${PA:-0}"
case "$BUMP" in patch) PA=$((PA+1));; minor) MI=$((MI+1)); PA=0;; major) MA=$((MA+1)); MI=0; PA=0;; none) ;; *) echo "--bump patch|minor|major|none"; exit 2;; esac
VERSION="$MA.$MI.$PA"
echo "$VERSION" > "$APP/VERSION"
trap 'echo "The release failed: putting the version back to $OLD."; echo "$OLD" > "$APP/VERSION"' ERR
echo "Releasing $OLD → $VERSION"

"$APP/build_app.sh"
trap - ERR

PY="$APP/.buildenv/bin/python"
OUT="$APP/release"; mkdir -p "$OUT/$VERSION"
ZIPNAME="${ZIP_NAME_TEMPLATE//\{version\}/$VERSION}"
ZIPURL="${ZIP_URL_TEMPLATE//\{version\}/$VERSION}"
cp "$APP/dist/Journal-Watcher-$VERSION.zip" "$OUT/$VERSION/$ZIPNAME"
cp "$APP/dist/Journal Watcher.dmg" "$OUT/$VERSION/Journal Watcher.dmg"
ARGS=(--version "$VERSION" --zip "$OUT/$VERSION/$ZIPNAME" --url "$ZIPURL" --previous "$OUT/appcast.json" --out "$OUT/$VERSION/appcast.json")
if [ -n "$NOTES_FILE" ]; then ARGS+=(--notes-file "$NOTES_FILE"); else ARGS+=(--notes "$NOTES"); fi
"$PY" "$APP/make_appcast.py" "${ARGS[@]}"
cp "$OUT/$VERSION/appcast.json" "$OUT/appcast.json"

if [ -n "$PUBLISH_DIR" ]; then
  mkdir -p "$PUBLISH_DIR"
  cp "$OUT/$VERSION/$ZIPNAME" "$PUBLISH_DIR/$ZIPNAME"           # first the file the feed points to…
  cp "$OUT/$VERSION/Journal Watcher.dmg" "$PUBLISH_DIR/Journal Watcher.dmg"
  cp "$OUT/appcast.json" "$PUBLISH_DIR/appcast.json"             # …then the feed itself, so nobody is told about a file that is not there yet
  echo "Published to: $PUBLISH_DIR"
else
  echo "PUBLISH_DIR is not set in app/release.conf: the release is in $OUT/$VERSION; copy appcast.json, the zip and the dmg to where the feed address points."
fi
echo "Done: version $VERSION. Installed copies show 'Update to $VERSION' within about six hours, or at once with Help → Check for Updates…"
