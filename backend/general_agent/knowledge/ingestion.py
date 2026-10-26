"""复用文档解析及切片算法，逐阶段持久发布知识库处理进度。"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import math
from typing import Any, TYPE_CHECKING
from uuid import UUID

from psycopg.types.json import Jsonb

from general_agent.attachments.documents import parse_document, MAX_TEXT_CHARACTERS
from general_agent.attachments.document_images import DocumentVision, ParsedImage, VISION_PROMPT_VERSION
from general_agent.attachments.knowledge import store_chunks, store_image_chunks, text_units
from general_agent.attachments.pagination import paginate_word
from general_agent.infrastructure.calls import BoundCalls
from general_agent.infrastructure.settings import EmbeddingSettingsStore

if TYPE_CHECKING:
    from general_agent.knowledge.service import KnowledgeService


def ingest(service: 'KnowledgeService', row: dict[str, Any]) -> None:
    """按持久化尝试身份处理文件，单图和向量失败保留可检索内容。"""
    identifier, attempt = row['id'], row['attempt']
    with service.calls.operations.operation('knowledge_ingestion', [str(identifier), str(attempt)], account_id=row['user_id']) as (operation, _):
        calls = service.calls.bind(operation)
        with service.database.connect() as connection:
            connection.execute("UPDATE knowledge_documents SET operation_id=%s WHERE id=%s AND attempt=%s AND status='processing'",
                               (operation.operation_id, identifier, attempt))
        try:
            process(service, row, calls)
        except LookupError:
            # 删除先取消外部调用；在同一任务锁内收尾，不保留迟到的派生内容。
            current = service.current(identifier)
            if current and current['status'] == 'deleting':
                service.cleanup(identifier)
        except Exception as error:
            message = str(error) if isinstance(error, ValueError) and not isinstance(error, UnicodeDecodeError) else '文档处理失败，请检查格式或模型配置后重试。'
            with service.database.connect() as connection:
                connection.execute(
                    "UPDATE knowledge_documents SET status='failed',error=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s AND attempt=%s AND status='processing'",
                    (message, identifier, attempt),
                )


def process(service: 'KnowledgeService', row: dict[str, Any], calls: BoundCalls) -> None:
    """固定计数快照，先完成原生/OCR 结构与插图描述，再写独立切片及索引。"""
    policy = service.policy
    identifier, attempt = row['id'], row['attempt']
    folder = service.directory / str(identifier)
    original, images_directory = folder / 'original', folder / 'images'
    extension = Path(row['filename']).suffix.lower()
    service.progress(identifier, attempt, 'parsing')
    if extension in {'.pdf', '.docx'}:
        def ocr_progress(completed: int, total: int) -> None:
            """逐页发布实际 OCR 进度并核验原处理尝试，删除后停止继续识别。"""
            service.progress(identifier, attempt, 'ocr', completed, total)

        document = parse_document(original, extension, images_directory,
                                  ocr_transport=service.transport, ocr_progress=ocr_progress)
        if document.error:
            raise ValueError(document.error)
        if extension == '.docx':
            pages, positions, image_positions = paginate_word(original, folder / 'paginated.pdf')
            if len(positions) != len(document.units):
                raise ValueError('Word 正文和分页位置不一致。')
            for index, unit in enumerate(document.units, 1):
                unit.page, unit.end_page = positions[index]
            for image in document.images:
                image.page = image_positions.get(image.ordinal)
                image.end_page = image.page
            document.info.total_units = pages
        units, parsed, info = document.units, document.images, document.info.model_dump()
    else:
        text = original.read_bytes().decode('utf-8-sig')
        if not text.strip() or any(ord(char) < 32 and char not in '\n\r\t' for char in text):
            raise ValueError('文件没有可读取文字或包含二进制控制字符，请上传 UTF-8 文本。')
        if len(text) > MAX_TEXT_CHARACTERS:
            raise ValueError('文档文字超过 2,000,000 字符，请拆分后上传。')
        units, parsed = text_units(text), []
        info = {'format': extension.lstrip('.'), 'total_units': len(units)}
    vision = DocumentVision(service.settings_directory, service.files, service.transport, calls)
    images = describe_images(service, row, parsed, vision)
    failures = sum(image['vision_status'] == 'failed' for image in images)
    if not any(unit.content.strip() for unit in units) and not any(image['vision_status'] == 'complete' for image in images):
        raise ValueError('文档没有可用正文，图片描述也未完成，请检查视觉模型后重试。')
    info.update(image_count=len(images), image_failed_count=failures, vision_revision=vision.revision,
                vision_rules=VISION_PROMPT_VERSION,
                chunk_layout=policy.layout_signature, tokenizer_model=policy.counter.model,
                token_count_method=policy.counter.method, tokenizer_fingerprint=policy.counter.fingerprint)
    service.progress(identifier, attempt, 'chunking')
    with service.database.connect() as connection:
        live = connection.execute("SELECT id FROM knowledge_documents WHERE id=%s AND attempt=%s AND status='processing' FOR UPDATE", (identifier, attempt)).fetchone()
        if not live:
            raise LookupError('文档处理已撤销。')
        # 相同正文的向量可在失败重试时复用；切片重建事务不会留下半份正文。
        cached = connection.execute('SELECT ordinal,content,embedding,embedding_revision FROM knowledge_chunks WHERE document_id=%s', (identifier,)).fetchall()
        store_chunks(connection, identifier, units, policy, knowledge=True, images=images)
        store_image_chunks(connection, identifier, images, policy, knowledge=True)
        for chunk in cached:
            if chunk['embedding'] is not None:
                connection.execute('UPDATE knowledge_chunks SET embedding=%s,embedding_revision=%s WHERE document_id=%s AND ordinal=%s AND content=%s',
                                   (Jsonb(chunk['embedding']), chunk['embedding_revision'], identifier, chunk['ordinal'], chunk['content']))
        connection.execute('UPDATE knowledge_documents SET parse_info=%s,chunk_policy=%s WHERE id=%s AND attempt=%s',
                           (Jsonb(info), policy.signature, identifier, attempt))
    warnings = [f'{failures} 张图片描述失败，可重试。'] if failures else []
    if info.get('ocr_failed_pages'):
        warnings.append(f'{len(info["ocr_failed_pages"])} 页 OCR 失败，可重试。')
    if info.get('ocr_low_confidence_pages'):
        warnings.append('部分扫描页识别置信度较低，请核对原文。')
    try:
        index_document(service, identifier, attempt, calls)
    except LookupError:
        raise
    except Exception:
        warnings.append('向量索引失败，当前使用关键词检索，可重试。')
    service.progress(identifier, attempt, 'complete', 1, 1)
    with service.database.connect() as connection:
        connection.execute(
            "UPDATE knowledge_documents SET status='ready',warning=%s,error=NULL,updated_at=CURRENT_TIMESTAMP "
            "WHERE id=%s AND attempt=%s AND status='processing' AND EXISTS(SELECT 1 FROM knowledge_bases b WHERE b.id=knowledge_base_id AND NOT b.deleting)",
            (' '.join(warnings) or None, identifier, attempt),
        )


def describe_images(service: 'KnowledgeService', row: dict[str, Any], parsed: list[ParsedImage], vision: DocumentVision) -> list[dict[str, Any]]:
    """有界并发保存图片描述，仅复用原图、图注及提示词版本均一致的成功结果。"""
    identifier, attempt = row['id'], row['attempt']
    folder = service.directory / str(identifier) / 'images'
    with service.database.connect() as connection:
        existing = connection.execute('SELECT metadata,provider_file,vision_status FROM knowledge_document_images WHERE document_id=%s', (identifier,)).fetchall()
    by_ordinal = {item['metadata']['ordinal']: item for item in existing}
    service.progress(identifier, attempt, 'images', 0, len(parsed))

    def describe(image: ParsedImage) -> tuple[dict[str, Any], bool]:
        """返回描述及本次是否新上传；表格截图直接完成本地兜底，不发送视觉模型。"""
        item = image.model_dump(mode='json')
        if image.media_type == 'table_preview':
            item.update(description='表格原区域预览', provider_file=None, vision_status='complete',
                        vision_model='table-preview', vision_revision='table-preview-v1', vision_error=None)
            return item, False
        previous = by_ordinal.get(image.ordinal)
        if previous:
            old = previous['metadata']
            if (old['sha256'] == image.sha256 and old.get('caption') == image.caption
                    and previous['vision_status'] == 'complete' and old.get('vision_revision') == vision.revision):
                (folder / image.storage_name).unlink(missing_ok=True)
                return {**old, 'provider_file': previous['provider_file'], 'vision_status': 'complete'}, False
            # 覆盖远端身份前先完成旧文件清理；失败时保留原记录供重试，不遗失删除凭据。
            if previous['provider_file']:
                vision.files.delete(previous['provider_file'])
            item['id'] = old['id']
            if old['storage_name'] != image.storage_name:
                (folder / old['storage_name']).unlink(missing_ok=True)
        current = service.current(identifier)
        if not current or current['status'] != 'processing' or current['attempt'] != attempt or current['deleting']:
            raise LookupError('文档处理已撤销。')
        remote: dict[str, Any] | None = None
        try:
            description, remote = vision.describe(folder / image.storage_name, image)
            item.update(description=description, provider_file=remote, vision_status='complete', vision_revision=vision.revision)
        except Exception:
            item.update(vision_status='failed', vision_error='图片描述未完成，请重试。', vision_revision=vision.revision)
        current = service.current(identifier)
        if not current or current['status'] != 'processing' or current['attempt'] != attempt or current['deleting']:
            if remote:
                vision.discard(remote)
            raise LookupError('文档处理已撤销。')
        return item, remote is not None

    results: list[dict[str, Any]] = []
    aborted = False
    first_error: Exception | None = None
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix='knowledge-image') as executor:
        pending = [executor.submit(describe, image) for image in parsed]
        for future in as_completed(pending):
            try:
                item, created_remote = future.result()
            except Exception as error:
                aborted = True
                first_error = first_error or error
                continue
            if aborted:
                if created_remote and item.get('provider_file'):
                    vision.discard(item['provider_file'])
                continue
            with service.database.connect() as connection:
                live = connection.execute("SELECT id FROM knowledge_documents WHERE id=%s AND attempt=%s AND status='processing' FOR UPDATE", (identifier, attempt)).fetchone()
                if not live:
                    if created_remote and item.get('provider_file'):
                        vision.discard(item['provider_file'])
                    aborted = True
                    continue
                connection.execute(
                    'INSERT INTO knowledge_document_images(id,document_id,ordinal,metadata,provider_file,vision_status) VALUES(%s,%s,%s,%s,%s,%s) '
                    'ON CONFLICT(document_id,ordinal) DO UPDATE SET metadata=EXCLUDED.metadata,provider_file=EXCLUDED.provider_file,vision_status=EXCLUDED.vision_status',
                    (UUID(item['id']), identifier, item['ordinal'], Jsonb({key: value for key, value in item.items() if key != 'provider_file'}),
                     Jsonb(item['provider_file']) if item.get('provider_file') else None, item['vision_status']),
                )
            results.append(item)
            try:
                service.progress(identifier, attempt, 'images', len(results), len(parsed))
            except LookupError:
                aborted = True
    if aborted:
        raise first_error or LookupError('文档处理已撤销。')
    results.sort(key=image_order)
    return results


def image_order(image: dict[str, Any]) -> int:
    """以原文图片序号恢复并发处理前的文档顺序。"""
    return int(image['ordinal'])


def index_document(service: 'KnowledgeService', identifier: UUID, attempt: UUID, calls: BoundCalls) -> None:
    """入库时分批构建当前配置向量，完成数反映已持久保存的实际切片数量。"""
    settings = EmbeddingSettingsStore(service.settings_directory).read()
    if settings is None:
        raise ValueError('embedding_unavailable')
    with service.database.connect() as connection:
        rows = connection.execute('SELECT ordinal,content,embedding,embedding_revision FROM knowledge_chunks WHERE document_id=%s ORDER BY ordinal', (identifier,)).fetchall()
    missing = [chunk for chunk in rows if chunk['embedding'] is None or chunk['embedding_revision'] != settings.revision]
    completed = len(rows) - len(missing)
    service.progress(identifier, attempt, 'indexing', completed, len(rows))
    for offset in range(0, len(missing), 16):
        service.progress(identifier, attempt, 'indexing', completed, len(rows))
        batch = missing[offset:offset + 16]
        result = calls.embed(settings, [chunk['content'] for chunk in batch])
        current = EmbeddingSettingsStore(service.settings_directory).read()
        if current is None or current.revision != settings.revision:
            raise ValueError('embedding_configuration_changed')
        with service.database.connect() as connection:
            for item in result.data:
                if not math.hypot(*item.embedding):
                    raise ValueError('embedding_unavailable')
                chunk = batch[item.index]
                connection.execute(
                    "UPDATE knowledge_chunks c SET embedding=%s,embedding_revision=%s FROM knowledge_documents d "
                    "WHERE c.document_id=%s AND c.ordinal=%s AND c.content=%s AND d.id=c.document_id AND d.status='processing' AND d.attempt=%s",
                    (Jsonb(item.embedding), settings.revision, identifier, chunk['ordinal'], chunk['content'], attempt),
                )
        completed += len(batch)
        service.progress(identifier, attempt, 'indexing', completed, len(rows))
    with service.database.connect() as connection:
        connection.execute("UPDATE knowledge_documents SET index_revision=%s WHERE id=%s AND attempt=%s AND status='processing'",
                           (settings.revision, identifier, attempt))
