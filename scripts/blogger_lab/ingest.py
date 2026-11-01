# -*- coding: utf-8 -*-
"""
scripts/blogger_lab/ingest.py
V7 博主实验室资料解析与全区间覆盖分块引擎
支持 TXT / Markdown / DOCX（正文与表格按序抽取），严格验证文本覆盖完整性，绝不截断丢弃。
"""

from __future__ import annotations
import os
import sys
import re
import hashlib
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
if os.path.join(_BASE, 'scripts') not in sys.path:
    sys.path.insert(0, os.path.join(_BASE, 'scripts'))

from blogger_lab.schemas import Source, Chunk

SUPPORTED_EXTENSIONS = {
    ".txt": "TXT",
    ".md": "MD",
    ".markdown": "MD",
    ".docx": "DOCX"
}

CONVERSION_REQUIRED_EXTENSIONS = {
    ".doc", ".pdf", ".rtf", ".wps", ".pages"
}


def compute_file_sha256(file_path: Path) -> str:
    """计算文件 SHA256 哈希"""
    h = hashlib.sha256()
    with file_path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_text_content(text: str) -> str:
    """规范化文本：统一换行、去除无效控制字符，保留中文标点与排版"""
    if not text:
        return ""
    # 统一换行符
    s = text.replace("\r\n", "\n").replace("\r", "\n")
    # 清理非打印控制字符（保留 \n, \t）
    s = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', s)
    return s


def parse_docx_xml_content(docx_path: Path) -> Tuple[str, List[str]]:
    """
    轻量原生 DOCX 解析器（基于 zipfile 与 ElementTree）。
    按顺序提取段落 (<w:p>) 与表格 (<w:tbl>)，记录未解析部件（图片/文本框等）。
    """
    unsupported_parts: List[str] = []
    text_blocks: List[str] = []

    try:
        with zipfile.ZipFile(docx_path, 'r') as zf:
            if 'word/document.xml' not in zf.namelist():
                raise ValueError("DOCX 结构异常：缺少 word/document.xml")

            xml_content = zf.read('word/document.xml')
            root = ET.fromstring(xml_content)

            # XML 命名空间定义
            ns = {
                'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
                'a': 'http://schemas.openxmlformats.org/drawingml/2006/main'
            }

            # 遍历 body 下的顶层子节点，确保段落与表格顺序一致
            body = root.find('w:body', ns)
            if body is None:
                return "", ["空文档：缺少 w:body"]

            for child in body:
                tag = child.tag.split('}')[-1]

                if tag == 'p':  # 段落
                    para_texts = []
                    for t in child.findall('.//w:t', ns):
                        if t.text:
                            para_texts.append(t.text)
                    line = "".join(para_texts).strip()
                    if line:
                        text_blocks.append(line)

                    # 检查是否有嵌入图片或文本框
                    if child.findall('.//w:drawing', ns):
                        unsupported_parts.append("段落中包含嵌入图片/图表 (已跳过视觉元素)")
                    if child.findall('.//w:txbxContent', ns):
                        unsupported_parts.append("段落中包含独立浮动文本框 (已跳过文本框)")

                elif tag == 'tbl':  # 表格
                    table_rows = []
                    for row in child.findall('.//w:tr', ns):
                        cell_texts = []
                        for cell in row.findall('.//w:tc', ns):
                            c_p_texts = []
                            for t in cell.findall('.//w:t', ns):
                                if t.text:
                                    c_p_texts.append(t.text)
                            cell_texts.append("".join(c_p_texts).strip())
                        if any(cell_texts):
                            table_rows.append("\t".join(cell_texts))
                    if table_rows:
                        text_blocks.append("\n".join(table_rows))

    except Exception as e:
        raise ValueError(f"DOCX 文件解析错误: {e}")

    # 去重未支持部件提示
    unsupported_unique = sorted(set(unsupported_parts))
    full_text = "\n\n".join(text_blocks)
    return full_text, unsupported_unique


def extract_text_from_file(file_path: Path) -> Tuple[str, str, List[str], Optional[str]]:
    """
    统一文件抽取接口。
    返回 (extracted_text, file_type, unsupported_parts, error_message)
    """
    suffix = file_path.suffix.lower()

    if suffix in CONVERSION_REQUIRED_EXTENSIONS:
        return (
            "",
            "UNSUPPORTED",
            [f"格式限制: 文件 {file_path.name} 为 {suffix} 格式，请转换为标准 .docx 或 .txt 后再导入"],
            "文件需格式转换"
        )

    if suffix not in SUPPORTED_EXTENSIONS:
        return (
            "",
            "UNSUPPORTED",
            [f"格式限制: 不支持的扩展名 {suffix}"],
            "不支持的文件格式"
        )

    file_type = SUPPORTED_EXTENSIONS[suffix]

    if file_type in ("TXT", "MD"):
        try:
            # 尝试标准 UTF-8 / UTF-8-SIG 读取
            raw_bytes = file_path.read_bytes()
            try:
                raw_text = raw_bytes.decode("utf-8-sig")
            except UnicodeDecodeError:
                # 降级尝试 GB18030
                raw_text = raw_bytes.decode("gb18030")
            clean_text = normalize_text_content(raw_text)
            if not clean_text.strip():
                return "", file_type, [], "文件正文为空或仅包含空白字符，不可作为提炼输入"
            return clean_text, file_type, [], None
        except Exception as e:
            return "", file_type, [], f"文本读取解码失败: {e}"

    elif file_type == "DOCX":
        try:
            text, unsupported = parse_docx_xml_content(file_path)
            clean_text = normalize_text_content(text)
            if not clean_text.strip():
                return "", file_type, unsupported, "DOCX 文件无有效正文内容"
            return clean_text, file_type, unsupported, None
        except Exception as e:
            return "", file_type, [], str(e)

    return "", "UNSUPPORTED", [], "未知文件类型"


def chunk_normalized_text(
    full_text: str,
    source_id: str,
    persona_id: str,
    batch_id: str,
    target_chunk_chars: int = 6000,
    overlap_chars: int = 300
) -> List[Chunk]:
    """
    全区间严格覆盖分块算法 (Coverage-preserving chunking)。
    优先按自然段落断句切分，若遇超长单段则硬切。
    严格保证 [0, total_chars] 100% 覆盖，相邻块存在重叠，末段绝无遗漏截断。
    """
    total_len = len(full_text)
    if total_len == 0 or not full_text.strip():
        return []

    if total_len <= target_chunk_chars:
        sha = hashlib.sha256(full_text.encode("utf-8")).hexdigest()
        chunk_id = f"chunk_{source_id}_000"
        return [
            Chunk(
                id=chunk_id,
                source_id=source_id,
                persona_id=persona_id,
                batch_id=batch_id,
                chunk_index=0,
                start_char=0,
                end_char=total_len,
                char_length=total_len,
                text_sha256=sha,
                text=full_text,
                status="PENDING",
                extraction_key=f"{source_id}_000_{sha[:12]}"
            )
        ]

    chunks: List[Chunk] = []
    start = 0
    chunk_idx = 0

    while start < total_len:
        # 预估理想结束点
        target_end = min(start + target_chunk_chars, total_len)

        if target_end >= total_len:
            actual_end = total_len
        else:
            # 在 target_end 附近寻找最近的段落换行点 \n
            search_window_start = max(start + target_chunk_chars - 500, start + 100)
            search_window_end = min(start + target_chunk_chars + 500, total_len)
            candidate_text = full_text[search_window_start:search_window_end]

            last_newline = candidate_text.rfind('\n')
            if last_newline != -1:
                actual_end = search_window_start + last_newline + 1
            else:
                # 寻找句号、分号等标点
                punct_match = list(re.finditer(r'[。！？；!?;]', candidate_text))
                if punct_match:
                    actual_end = search_window_start + punct_match[-1].end()
                else:
                    actual_end = target_end

        chunk_str = full_text[start:actual_end]
        c_sha = hashlib.sha256(chunk_str.encode("utf-8")).hexdigest()
        c_id = f"chunk_{source_id}_{str(chunk_idx).zfill(3)}"

        chunk_obj = Chunk(
            id=c_id,
            source_id=source_id,
            persona_id=persona_id,
            batch_id=batch_id,
            chunk_index=chunk_idx,
            start_char=start,
            end_char=actual_end,
            char_length=len(chunk_str),
            text_sha256=c_sha,
            text=chunk_str,
            status="PENDING",
            extraction_key=f"{source_id}_{str(chunk_idx).zfill(3)}_{c_sha[:12]}"
        )
        chunks.append(chunk_obj)
        chunk_idx += 1

        if actual_end >= total_len:
            break

        # 下一块起始点，带 overlap
        start = max(actual_end - overlap_chars, start + 1)

    # ==========================================
    # 覆盖完整性数学验证 (Fail-closed)
    # ==========================================
    if not chunks:
        raise ValueError("分块结果为空")

    if chunks[0].start_char != 0:
        raise ValueError(f"分块覆盖漏洞：首块未从 0 开始 (start={chunks[0].start_char})")

    if chunks[-1].end_char != total_len:
        raise ValueError(f"分块覆盖漏洞：末块未完整包含末尾 (end={chunks[-1].end_char}, total={total_len})")

    for i in range(len(chunks) - 1):
        if chunks[i + 1].start_char > chunks[i].end_char:
            raise ValueError(
                f"分块覆盖漏洞：块 {i} 与块 {i+1} 之间存在字符间隙 "
                f"(chunk_{i}.end={chunks[i].end_char} < chunk_{i+1}.start={chunks[i+1].start_char})"
            )

    return chunks
