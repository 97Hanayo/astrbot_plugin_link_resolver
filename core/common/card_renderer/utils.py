# region 导入
from __future__ import annotations

from pathlib import Path

from PIL import ImageFont

# endregion


# region 字体查找
def find_default_font() -> Path | None:
    """查找可用的中文字体，跨发行版兼容。

    优先级:
    1. astrbot_plugin_parser 插件内置的字体
    2. 常见中文字体路径（覆盖 Ubuntu/Debian/CentOS/Arch/macOS）
    3. fc-match :lang=zh 兜底（依赖 fontconfig，几乎所有桌面 Linux 默认装）
    """
    plugin_root = Path(__file__).resolve().parents[4]

    parser_resources = (
        plugin_root
        / "astrbot_plugin_parser"
        / "core"
        / "resources"
        / "HYSongYunLangHeiW-1.ttf"
    )
    if parser_resources.exists():
        return parser_resources

    system_fonts = [
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/wqy-microhei/wqy-microhei.ttc",
        "/usr/share/fonts/wqy-zenhei/wqy-zenhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
        "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf",
        "/usr/share/fonts/opentype/source-han-sans/SourceHanSansCN-Regular.otf",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
        "/usr/share/fonts/truetype/arphic/ukai.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/Library/Fonts/Songti.ttc",
        str(Path.home() / ".fonts/wqy-microhei.ttc"),
        str(Path.home() / ".fonts/wqy-zenhei.ttc"),
        str(Path.home() / ".local/share/fonts/wqy-microhei.ttc"),
        str(Path.home() / ".local/share/fonts/wqy-zenhei.ttc"),
    ]
    for font in system_fonts:
        if Path(font).exists():
            return Path(font)

    try:
        import subprocess

        result = subprocess.run(
            ["fc-match", "-f", "%{file}", ":lang=zh"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            candidate = Path(result.stdout.strip())
            if candidate.exists():
                return candidate
    except Exception:
        pass

    return None


# endregion


# region 字体加载
def load_font(
    font_path: Path | None, size: int
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """加载字体，如果路径不存在则使用默认字体"""
    if font_path and font_path.exists():
        return ImageFont.truetype(str(font_path), size)
    return ImageFont.load_default()


# endregion


# region 文本工具
def get_line_height(font: ImageFont.ImageFont) -> int:
    """获取行高"""
    ascent, descent = font.getmetrics()
    return ascent + descent


def get_text_width(font: ImageFont.ImageFont, text: str) -> int:
    """获取文本宽度"""
    return int(font.getlength(text))


def wrap_text(
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    """自动换行文本

    逐字符测量宽度，超出 max_width 时换行
    """
    if not text:
        return []

    lines: list[str] = []
    for raw in text.splitlines():
        current = ""
        for ch in raw:
            candidate = current + ch
            if current and get_text_width(font, candidate) > max_width:
                lines.append(current)
                current = ch
            else:
                current = candidate
        if current:
            lines.append(current)
    return lines


# endregion


# region 导出
__all__ = [
    "find_default_font",
    "load_font",
    "get_line_height",
    "get_text_width",
    "wrap_text",
]
# endregion
