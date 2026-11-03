#!/bin/bash
# 用 dpkg-deb 组装安装包。临时目录只放在 /tmp，打完即删。
set -eu
ROOT="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/yuketang_courseware/__init__.py")"
if [ -z "$VERSION" ]; then
  echo "读不到版本号" >&2
  exit 1
fi
STAGE="$(mktemp -d /tmp/yuketang-courseware-deb.XXXXXX)"
cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

mkdir -p \
  "$STAGE/DEBIAN" \
  "$STAGE/usr/bin" \
  "$STAGE/usr/share/applications" \
  "$STAGE/usr/share/man/man1" \
  "$STAGE/usr/share/icons/hicolor/scalable/apps" \
  "$STAGE/usr/share/doc/yuketang-courseware" \
  "$STAGE/usr/share/yuketang-courseware/yuketang_courseware"

find "$ROOT/yuketang_courseware" -maxdepth 1 -name '*.py' -exec cp {} "$STAGE/usr/share/yuketang-courseware/yuketang_courseware/" \;
find "$STAGE/usr/share/yuketang-courseware" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
install -m 0755 "$ROOT/packaging/yuketang-courseware" "$STAGE/usr/bin/yuketang-courseware"
install -m 0644 "$ROOT/packaging/yuketang-courseware.desktop" "$STAGE/usr/share/applications/yuketang-courseware.desktop"
gzip -9 -c "$ROOT/packaging/yuketang-courseware.1" > "$STAGE/usr/share/man/man1/yuketang-courseware.1.gz"
install -m 0644 "$ROOT/packaging/yuketang-courseware.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/yuketang-courseware.svg"
install -m 0644 "$ROOT/README.md" "$STAGE/usr/share/doc/yuketang-courseware/README.md"
find "$STAGE/usr/share/yuketang-courseware" -type d -exec chmod 0755 {} \;
find "$STAGE/usr/share/yuketang-courseware" -type f -exec chmod 0644 {} \;
chmod 0755 "$STAGE" "$STAGE/DEBIAN" "$STAGE/usr" "$STAGE/usr/bin" \
  "$STAGE/usr/share" "$STAGE/usr/share/applications" \
  "$STAGE/usr/share/man" "$STAGE/usr/share/man/man1" \
  "$STAGE/usr/share/icons" "$STAGE/usr/share/icons/hicolor" \
  "$STAGE/usr/share/icons/hicolor/scalable" \
  "$STAGE/usr/share/icons/hicolor/scalable/apps" \
  "$STAGE/usr/share/doc" "$STAGE/usr/share/doc/yuketang-courseware"
chmod 0644 \
  "$STAGE/usr/share/applications/yuketang-courseware.desktop" \
  "$STAGE/usr/share/man/man1/yuketang-courseware.1.gz" \
  "$STAGE/usr/share/icons/hicolor/scalable/apps/yuketang-courseware.svg" \
  "$STAGE/usr/share/doc/yuketang-courseware/README.md"

SIZE="$(du -sk "$STAGE/usr" | awk '{print $1}')"
cat > "$STAGE/DEBIAN/control" <<EOF
Package: yuketang-courseware
Version: ${VERSION}
Section: education
Priority: optional
Architecture: all
Depends: python3 (>= 3.8), python3-gi, gir1.2-gtk-4.0 (>= 4.6), gir1.2-adw-1 (>= 1.0), python3-pil
Recommends: microsoft-edge-stable | microsoft-edge | google-chrome-stable | google-chrome | chromium
Maintainer: Yuketang Courseware <noreply@localhost>
Installed-Size: ${SIZE}
Description: 下载雨课堂课堂幻灯片并合成 PDF
 供本人学习使用。在独立浏览器窗口登录后，勾选讲次，将幻灯片图片合成 PDF；
 也可以命令行整批下载。课件仅供本人学习，不要再传播。
EOF
chmod 0644 "$STAGE/DEBIAN/control"

mkdir -p "$ROOT/dist"
OUT="$ROOT/dist/yuketang-courseware_${VERSION}_all.deb"
rm -f "$OUT"
dpkg-deb --root-owner-group --build "$STAGE" "$OUT"
echo "$OUT"
