"""把幻灯片图片写成 PDF。

封面本来就是 JPEG，直接按 /DCTDecode 嵌进 PDF，不解码、不重编码，页数与画质
都不变。页尺寸统一按第一页，其余页等比缩放后居中铺白，不拉伸。
非 JPEG 页（PNG 等）才交给 Pillow 转成 JPEG；没装 Pillow 时该页记为失败，不
影响其他页。
只写结构，不写元数据，不写来源网址。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path


class PdfError(Exception):
    """可以直接显示给用户的错误。"""


@dataclass(frozen=True)
class Page:
    data: bytes
    width: int
    height: int
    components: int = 3


_EOF = b"%%EOF"
_PDF_PAGES = re.compile(rb"/Type\s*/Pages\b")
_COUNT = re.compile(rb"/Count\s+(\d+)")


def is_jpeg(data: bytes) -> bool:
    return data[:3] == b"\xff\xd8\xff"


def has_eoi(data: bytes, tail: int = 4096) -> bool:
    """结尾附近有没有 EOI（FFD9）。只有 SOI 没有 EOI 的，就是被截断的半张图。

    只看尾部一小段：EXIF 缩略图里也带 FFD9，全文搜索会把半张图误判成完整。
    """
    if len(data) < 2:
        return False
    start = max(2, len(data) - tail)
    return data.rfind(b"\xff\xd9", start) >= 0


def jpeg_size(data: bytes) -> tuple[int, int, int]:
    """读 JPEG 的 SOF 段，返回（宽，高，分量数）。不整幅解码。"""
    if not is_jpeg(data):
        raise PdfError("不是 JPEG 图片")
    index = 2
    total = len(data)
    while index + 4 <= total:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xFF, 0x00):
            index += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD9:
            index += 2
            continue
        length = (data[index + 2] << 8) | data[index + 3]
        if length < 2:
            index += 2
            continue
        is_sof = 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC)
        if is_sof:
            if index + 9 > total:
                raise PdfError("JPEG 头部被截断")
            height = (data[index + 5] << 8) | data[index + 6]
            width = (data[index + 7] << 8) | data[index + 8]
            components = data[index + 9] if index + 9 < total else 3
            if width <= 0 or height <= 0:
                raise PdfError("图片尺寸无效")
            return width, height, components
        index += 2 + length
    raise PdfError("JPEG 里没有尺寸段")


def page_from_bytes(data: bytes) -> Page:
    """JPEG 直取尺寸；其他格式交给 Pillow 转 JPEG。

    半张图（有 SOI 没 EOI）在这里就被拒掉：嵌进 PDF 只会得到一个打不开的文件，
    宁可少一页、在结果里写清楚缺了第几页。
    """
    if is_jpeg(data):
        width, height, components = jpeg_size(data)
        if not has_eoi(data):
            raise PdfError("图片不完整（缺结尾标记）")
        return Page(data=data, width=width, height=height, components=components)
    return _page_from_pillow(data)


def _page_from_pillow(data: bytes) -> Page:
    try:
        from PIL import Image
    except ImportError:
        raise PdfError("这一页不是 JPEG，且没有安装 python3-pil") from None
    try:
        with Image.open(BytesIO(data)) as image:
            image.load()
            flat = _flatten(image)
            try:
                buffer = BytesIO()
                flat.save(buffer, format="JPEG", quality=92)
                encoded = buffer.getvalue()
            finally:
                if flat is not image:
                    flat.close()
    except PdfError:
        raise
    except Exception:
        raise PdfError("图片解码失败") from None
    width, height, components = jpeg_size(encoded)
    return Page(data=encoded, width=width, height=height, components=components)


def _flatten(image):
    """带透明通道的铺白底，其余统一转 RGB。"""
    from PIL import Image

    has_alpha = image.mode in ("RGBA", "LA", "PA") or (
        image.mode == "P" and "transparency" in image.info
    )
    if has_alpha:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        rgba.close()
        return background
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


def _number(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text or "0"


def write_pdf(pages: list[Page], dest: Path) -> int:
    """写 PDF，返回页数。先写 .part 再改名，写坏也不留下半个文件。"""
    if not pages:
        raise PdfError("没有页面")
    base_w = pages[0].width
    base_h = pages[0].height
    count = len(pages)
    chunks: list[bytes] = []
    offsets: list[int] = []
    position = 0

    def put(block: bytes) -> None:
        nonlocal position
        chunks.append(block)
        position += len(block)

    def mark() -> None:
        offsets.append(position)

    put(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    mark()
    put(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
    kids = " ".join(f"{3 + index * 3} 0 R" for index in range(count))
    mark()
    put(f"2 0 obj\n<< /Type /Pages /Kids [{kids}] /Count {count} >>\nendobj\n".encode())

    for index, page in enumerate(pages):
        page_obj = 3 + index * 3
        content_obj = page_obj + 1
        image_obj = page_obj + 2
        if page.width == base_w and page.height == base_h:
            content = f"q {base_w} 0 0 {base_h} 0 0 cm /Im0 Do Q\n"
        else:
            scale = min(base_w / page.width, base_h / page.height)
            dx = (base_w - page.width * scale) / 2
            dy = (base_h - page.height * scale) / 2
            content = (
                f"q {_number(scale)} 0 0 {_number(scale)} "
                f"{_number(dx)} {_number(dy)} cm /Im0 Do Q\n"
            )
        content_bytes = content.encode()
        space = "DeviceCMYK" if page.components == 4 else "DeviceRGB"
        mark()
        put(
            (
                f"{page_obj} 0 obj\n<< /Type /Page /Parent 2 0 R "
                f"/MediaBox [0 0 {base_w} {base_h}] "
                f"/Resources << /XObject << /Im0 {image_obj} 0 R >> >> "
                f"/Contents {content_obj} 0 R >>\nendobj\n"
            ).encode()
        )
        mark()
        put(f"{content_obj} 0 obj\n<< /Length {len(content_bytes)} >>\nstream\n".encode())
        put(content_bytes)
        put(b"\nendstream\nendobj\n")
        mark()
        put(
            (
                f"{image_obj} 0 obj\n<< /Type /XObject /Subtype /Image "
                f"/Width {page.width} /Height {page.height} "
                f"/ColorSpace /{space} /BitsPerComponent 8 /Filter /DCTDecode "
                f"/Length {len(page.data)} >>\nstream\n"
            ).encode()
        )
        put(page.data)
        put(b"\nendstream\nendobj\n")

    xref = position
    trailer = [f"xref\n0 {len(offsets) + 1}\n", "0000000000 65535 f \n"]
    trailer.extend(f"{offset:010d} 00000 n \n" for offset in offsets)
    trailer.append(
        f"trailer\n<< /Size {len(offsets) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    )
    put("".join(trailer).encode())

    payload = b"".join(chunks)
    part = dest.with_suffix(dest.suffix + ".part")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(payload)
        os.replace(part, dest)
    except OSError as exc:
        part.unlink(missing_ok=True)
        raise PdfError("写 PDF 失败") from exc
    return count


def count_pages(path: Path) -> int | None:
    """读 PDF 头里的 /Count。读不到返回 None。

    /Count 不一定写在 /Type /Pages 后面（有的导出器把它写在前面），所以在
    页树对象前后各看一段；看不到就交给调用方退回 pdfinfo。
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(262144)
    except OSError:
        return None
    match = _PDF_PAGES.search(head)
    if match is None:
        return None
    start = max(0, match.start() - 1024)
    end = min(len(head), match.end() + 1024)
    found = _COUNT.search(head, start, end)
    if found is None:
        return None
    try:
        pages = int(found.group(1))
    except ValueError:
        return None
    return pages if pages >= 0 else None


def looks_complete(path: Path) -> bool:
    """文件头是 %PDF-、末尾有 %%EOF、页数至少 1 页，才当作已有文件跳过。"""
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if not path.is_file() or size < 1024:
        return False
    try:
        with path.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                return False
            handle.seek(max(0, size - 2048))
            if _EOF not in handle.read(2048):
                return False
    except OSError:
        return False
    pages = count_pages(path)
    return pages is None or pages >= 1
