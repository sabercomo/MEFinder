"""Page selection over normalized export data; never reparse source documents."""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Mapping, Sequence

from .document_export import DocumentExportError
from .export_footnotes import Footnote, FootnoteText, NormalizedDocument
from .markdown_export_normalize import ExportOptions, _physical_page, _printed_page_raw, resolve_page_marker


@dataclass(frozen=True)
class PageSelection:
    """Explicit printed labels or 1-based physical PDF pages."""
    mode: str
    labels: tuple[str, ...]
    expression: str

    @classmethod
    def from_mapping(cls, value: object) -> PageSelection:
        """Validate a bounded selection such as 12-18,25,iv."""
        if not isinstance(value, Mapping) or set(value) - {'mode', 'pages'}:
            raise DocumentExportError('页码选择必须包含 mode 和 pages。')
        mode, expression = value.get('mode'), value.get('pages')
        if mode not in ('printed', 'physical'):
            raise DocumentExportError('请选择原书页码或 PDF 物理页码。')
        if not isinstance(expression, str) or not expression.strip() or len(expression) > 512:
            raise DocumentExportError('请填写页码，例如 12-18,25；最长 512 字符。')
        labels = []
        for token in expression.replace('，', ',').split(','):
            token = token.strip()
            match = re.fullmatch(r'([0-9]{1,7})\s*[-–—]\s*([0-9]{1,7})', token)
            if match:
                start, end = map(int, match.groups())
                if start < 1 or end < start or end - start >= 10000:
                    raise DocumentExportError('页码范围须从小到大，单次最多选择 10000 页。')
                labels.extend(str(n) for n in range(start, end + 1))
            elif token and len(token) <= 64 and not re.search(r'[\s,\-–—<>\x00-\x1f]', token):
                if mode == 'physical' and (not token.isascii() or not token.isdigit() or int(token) < 1):
                    raise DocumentExportError('PDF 物理页码必须是从 1 开始的整数。')
                labels.append(_label(token))
            else:
                raise DocumentExportError('页码格式无效；数字范围用 12-18，其他页码标签请逐个填写。')
            if len(labels) > 10000:
                raise DocumentExportError('单次最多选择 10000 页。')
        return cls(mode, tuple(dict.fromkeys(labels)), expression.strip())


def _label(value: object) -> str:
    text = str(value or '').strip()
    return str(int(text)) if text.isascii() and text.isdigit() else text


def resolve_pdf_pages(pages: Sequence[Mapping[str, object]], selection: PageSelection) -> list[int]:
    """Resolve every requested label uniquely; never silently skip missing pages."""
    lookup: dict[str, list[int]] = {}
    for page in pages:
        physical = _physical_page(page)
        label = str(physical) if selection.mode == 'physical' and physical is not None else _printed_page_raw(page) if selection.mode == 'printed' else None
        if label is not None and physical is not None:
            lookup.setdefault(_label(label), []).append(physical)
    selected = set()
    for label in selection.labels:
        hits = lookup.get(label, [])
        if not hits:
            raise DocumentExportError(f'未找到页码「{label}」的已入库页面；请检查页码映射或改用 PDF 物理页码。')
        if len(hits) != 1:
            raise DocumentExportError(f'页码「{label}」对应多个页面，请改用 PDF 物理页码。')
        page = next(p for p in pages if _physical_page(p) == hits[0])
        if selection.mode == 'printed' and (page.get('layout_mode') == 'spread' or page.get('page_scope') == 'spread'):
            raise DocumentExportError('该原书页位于合页 PDF 中，暂不支持按左右半页裁剪；请改用 PDF 物理页码导出整张页面。')
        selected.add(hits[0])
    return sorted(selected)


def select_normalized_pages(document: NormalizedDocument, pages: Sequence[Mapping[str, object]],
                            selected: Sequence[int], options: ExportOptions) -> NormalizedDocument:
    """Keep selected body blocks and their notes at the existing chapter ends."""
    if document.reconstruction_report.get('retained_block_count', 0):
        raise DocumentExportError('存在无法确定页码归属的跨页文本，暂不能精确按页导出；可使用整书导出。')
    wanted = set(selected)
    texts = [item for item in document.items if isinstance(item, FootnoteText)
             and item.export_physical_page in wanted]
    note_ids = {ref.note_id for item in texts for ref in item.references}
    definitions = {item.note_id for item in document.items if isinstance(item, Footnote)}
    if note_ids - definitions:
        raise DocumentExportError('选中正文存在缺失的脚注定义，未生成不完整的导出文件。')
    page_by_number = {_physical_page(page): page for page in pages}
    result = []
    previous = None
    for item in document.items:
        if isinstance(item, Footnote):
            if item.note_id in note_ids:
                result.append(item)
        elif isinstance(item, FootnoteText) and item.export_physical_page in wanted:
            if item.export_physical_page != previous:
                marker = resolve_page_marker(page_by_number[item.export_physical_page], options)
                if marker is not None:
                    result.append(marker)
                previous = item.export_physical_page
            result.append(item)
    # Empty parsed pages are allowed; counts describe selected source pages.
    report = {**document.footnote_report, 'report_scope': 'whole_document',
              'selection': {'physical_pages': list(selected), 'body_block_count': len(texts),
                            'reference_count': sum(len(t.references) for t in texts),
                            'note_count': len(note_ids)}}
    return replace(document, items=result, footnote_report=report)


def select_epub_paragraphs(paragraphs: Sequence[Mapping[str, object]], selection: PageSelection) -> list:
    """Use publisher page labels only; imported EPUB links are not reconstructed."""
    if selection.mode != 'printed':
        raise DocumentExportError('EPUB 没有 PDF 物理页码，请选择出版方原书页码。')
    groups: dict[str, list] = {}
    previous = None
    duplicates = set()
    for paragraph in paragraphs:
        label = _label(paragraph.get('original_page_start'))
        if label and paragraph.get('page_source_type') in ('epub_page_list', 'epub_pagebreak'):
            end = _label(paragraph.get('original_page_end'))
            if end and end != label:
                raise DocumentExportError('EPUB 含跨页段落且缺少页内字符边界，暂不能精确按页导出。')
            if label != previous and label in groups:
                duplicates.add(label)
            groups.setdefault(label, []).append(paragraph)
            previous = label
        else:
            previous = None
    for label in selection.labels:
        if label not in groups or label in duplicates:
            raise DocumentExportError(f'EPUB 页码「{label}」缺少唯一的出版方分页标记，不能推算页码。')
    chosen = {id(p) for label in selection.labels for p in groups[label]}
    return [p for p in paragraphs if id(p) in chosen]
