#!/bin/bash
# End-to-end test of "Update now" with the real app (about five minutes; it overwrites app/dist, so run it before a release build, not after):
# installs build A in a scratch folder, offers build B through a local feed, presses Update now over the app's own API, and checks that the installed app
# became B and opened again.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"; APP="$ROOT/app"; NAME="Journal Watcher"
OLDV="$(tr -d '[:space:]' < "$APP/VERSION")"
IFS=. read -r MA MI PA <<< "$OLDV"; NEWV="$MA.$MI.$((PA+1))"
T="$(mktemp -d)"; FEEDPORT=9779
cleanup() { echo "$OLDV" > "$APP/VERSION"; pkill -f "$T/install/$NAME.app/Contents/MacOS" 2>/dev/null || true; kill ${FEEDPID:-0} 2>/dev/null || true; rm -rf "$T"; }
trap cleanup EXIT
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
PYB="$APP/.buildenv/bin/python"

say "A: build $OLDV and install it in $T/install"
"$APP/build_app.sh" --quick > "$T/buildA.log" 2>&1 || { tail -30 "$T/buildA.log"; exit 1; }
mkdir -p "$T/install" "$T/feed"; ditto "$APP/dist/$NAME.app" "$T/install/$NAME.app"

say "B: build $NEWV and publish it to a local feed"
echo "$NEWV" > "$APP/VERSION"
"$APP/build_app.sh" --quick > "$T/buildB.log" 2>&1 || { tail -30 "$T/buildB.log"; exit 1; }
ditto -c -k --keepParent "$APP/dist/$NAME.app" "$T/feed/Journal-Watcher-$NEWV.zip"
"$PYB" "$APP/make_appcast.py" --version "$NEWV" --zip "$T/feed/Journal-Watcher-$NEWV.zip" --url "http://127.0.0.1:$FEEDPORT/Journal-Watcher-$NEWV.zip" --notes "End-to-end test build" --out "$T/feed/appcast.json"
echo "$OLDV" > "$APP/VERSION"
(cd "$T/feed" && python3 -m http.server $FEEDPORT --bind 127.0.0.1 > "$T/feed.log" 2>&1) & FEEDPID=$!
sleep 1

say "run A, ask the feed, press Update now"
JW_HOME="$T/home" JW_UPDATE_FEED="http://127.0.0.1:$FEEDPORT/appcast.json" JW_NO_UPDATE_CHECK=1 "$T/install/$NAME.app/Contents/MacOS/$NAME" > "$T/app.log" 2>&1 &
APID=$!
for i in $(seq 1 60); do curl -sf http://127.0.0.1:8790/api/update >/dev/null && break; sleep 1; done
J() { python3 -c "import json,sys; d=json.load(sys.stdin); print(d$1)"; }
CUR="$(curl -s http://127.0.0.1:8790/api/update | J "['current']")"
[ "$CUR" = "$OLDV" ] || { echo "FAIL: the installed app reports $CUR, not $OLDV"; exit 1; }
CHK="$(curl -s -X POST -d '{}' http://127.0.0.1:8790/api/update/check)"
echo "$CHK" | J "['latest']" | grep -qx "$NEWV" || { echo "FAIL: the feed was not seen: $CHK"; exit 1; }
echo "$CHK" | J "['can_install']" | grep -qx True || { echo "FAIL: this copy says it cannot install itself: $CHK"; exit 1; }
curl -s -X POST -d '{}' http://127.0.0.1:8790/api/update/install > /dev/null
for i in $(seq 1 90); do
  V="$(defaults read "$T/install/$NAME.app/Contents/Info" CFBundleShortVersionString 2>/dev/null || true)"
  [ "$V" = "$NEWV" ] && break
  kill -0 $APID 2>/dev/null || sleep 1
  sleep 1
done
V="$(defaults read "$T/install/$NAME.app/Contents/Info" CFBundleShortVersionString)"
[ "$V" = "$NEWV" ] || { echo "FAIL: the installed app is still $V"; cat "$HOME/Library/Application Support/Journal Watcher/updates/swap.log" 2>/dev/null | tail -20; exit 1; }
sleep 4
pgrep -f "$T/install/$NAME.app/Contents/MacOS" > /dev/null || { echo "FAIL: the new app did not open"; tail -20 "$HOME/Library/Application Support/Journal Watcher/updates/swap.log"; exit 1; }
[ ! -d "$T/install/$NAME.app.old-"* ] 2>/dev/null || { echo "FAIL: the old copy was left behind"; exit 1; }
codesign --verify --deep --strict "$T/install/$NAME.app" && echo "the new app's signature is valid"
say "PASS: $OLDV updated itself to $NEWV and reopened"
