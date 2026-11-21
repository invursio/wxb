#!/usr/bin/env bash
# Build a WXB Studio AppImage.
#
# The AppImage carries the application, its pure-Python input dependencies and
# the launcher.  GTK4, libadwaita, PyGObject, pycairo and the optional system
# tools stay with the distribution, matching how the app is documented.
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
VERSION=${WXB_VERSION:-$(git -C "$ROOT" describe --tags --always --dirty 2>/dev/null || echo 0.1.0)}
VERSION=${VERSION#v}
ARCH=${ARCH:-x86_64}
BUILD=${BUILD_DIR:-"$ROOT/build/appimage"}
APPDIR="$BUILD/WXB.AppDir"
DIST="$ROOT/dist"
TOOL="$BUILD/appimagetool-$ARCH.AppImage"

case "$ARCH" in
    x86_64) TOOL_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage" ;;
    aarch64) TOOL_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-aarch64.AppImage" ;;
    *) echo "unsupported ARCH: $ARCH" >&2; exit 2 ;;
esac

rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/share/wxb" "$APPDIR/usr/bin" \
         "$APPDIR/usr/share/icons/hicolor/scalable/apps" \
         "$APPDIR/usr/share/applications" "$APPDIR/usr/share/metainfo" "$DIST"

# Application source (tests, artifacts and backups are repository material,
# not runtime payload).
for path in wxb.py wxb_engine.py wxb_input_lab.py wxb_macros.py wxb_overlay.py \
            wxb_recorder.py wxb_studio.py wxb_studio_service.py \
            roblox_runtime.py autoswitcher.py requirements.txt; do
    cp "$ROOT/$path" "$APPDIR/usr/share/wxb/"
done
cp -a "$ROOT/lution_core" "$APPDIR/usr/share/wxb/"
find "$APPDIR/usr/share/wxb" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$APPDIR/usr/share/wxb" -name '*.pyc' -delete

# Pure-Python runtime dependencies.  Versions are pinned so a build does not
# silently pick up a new API; all four packages are BSD/MIT/LGPL-compatible.
python3 -m pip install --disable-pip-version-check --no-compile --no-deps \
    --target "$APPDIR/usr/share/wxb/vendor" \
    pynput==1.8.2 python-xlib==0.33 six==1.16.0 keyboard==0.13.5

install -m 0755 "$ROOT/packaging/appimage/AppRun" "$APPDIR/AppRun"
ln -s ../../AppRun "$APPDIR/usr/bin/wxb"

install -m 0644 "$ROOT/packaging/appimage/io.github.wxb.Studio.desktop" \
    "$APPDIR/io.github.wxb.Studio.desktop"
install -m 0644 "$ROOT/packaging/appimage/io.github.wxb.Studio.desktop" \
    "$APPDIR/usr/share/applications/io.github.wxb.Studio.desktop"
install -m 0644 "$ROOT/packaging/appimage/io.github.wxb.Studio.metainfo.xml" \
    "$APPDIR/usr/share/metainfo/io.github.wxb.Studio.metainfo.xml"
install -m 0644 "$ROOT/icons/hicolor/scalable/apps/io.github.wxb.Studio.svg" \
    "$APPDIR/io.github.wxb.Studio.svg"
install -m 0644 "$ROOT/icons/hicolor/scalable/apps/io.github.wxb.Studio.svg" \
    "$APPDIR/usr/share/icons/hicolor/scalable/apps/io.github.wxb.Studio.svg"
install -m 0644 "$ROOT/icons/hicolor/scalable/apps/io.github.wxb.Studio.Mono.svg" \
    "$APPDIR/usr/share/icons/hicolor/scalable/apps/io.github.wxb.Studio.Mono.svg"
install -m 0644 "$ROOT/LICENSE" "$APPDIR/usr/share/wxb/LICENSE"
install -m 0644 "$ROOT/lution_core/LICENSE.lution" \
    "$APPDIR/usr/share/wxb/LICENSE.lution"

if [ ! -x "$TOOL" ]; then
    echo "Fetching appimagetool ($ARCH)..."
    curl -fsSL -o "$TOOL" "$TOOL_URL"
    chmod +x "$TOOL"
fi

OUT="$DIST/WXB-${VERSION}-${ARCH}.AppImage"
rm -f "$OUT"
# --appimage-extract-and-run keeps the build working on hosts without FUSE.
ARCH="$ARCH" "$TOOL" --appimage-extract-and-run --no-appstream "$APPDIR" "$OUT"
chmod +x "$OUT"
echo "Built $OUT"
