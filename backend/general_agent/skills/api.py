"""提供制作任务、交付文件、隔离预览与管理员提供商配置接口。"""

import base64
import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from general_agent.auth.dependencies import current_user, require_admin
from general_agent.skills.runtime import SkillRuntime


class ProviderInput(BaseModel):
    """仅允许可选媒体提供商变量，空字符串保留原配置，删除必须明确列出。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    variables: dict[str, str] = Field(default_factory=dict)
    remove: list[str] = Field(default_factory=list)


def skill_router(runtime: SkillRuntime) -> APIRouter:
    """组装所属会话文件接口和按短期能力令牌授权的隔离预览。"""
    router = APIRouter()
    service = runtime.service

    @router.get('/api/skills')
    def catalog(request: Request) -> JSONResponse:
        """登录用户可查看可用 Skill 及完整描述，不执行任何制作工作。"""
        current_user(request)
        return JSONResponse(service.catalog.entries())

    @router.get('/api/conversations/{conversation}/skill-tasks')
    def tasks(conversation: UUID, request: Request) -> JSONResponse:
        """读取会话制作任务及真实交付清单，不返回内部工作消息。"""
        account = current_user(request).id
        try:
            rows = service.list(account, conversation)
            for row in rows:
                with service.database.connect() as connection:
                    row['artifacts'] = connection.execute('SELECT id,filename,created_at FROM skill_artifacts WHERE task_id=%s ORDER BY created_at DESC', (row['id'],)).fetchall()
            return JSONResponse(jsonable_encoder(rows))
        except LookupError as error:
            raise HTTPException(404, str(error)) from None

    @router.get('/api/conversations/{conversation}/skill-artifacts/{artifact}/file')
    def download(conversation: UUID, artifact: UUID, request: Request) -> FileResponse:
        """重新授权后下载不可变产物，SVG/HTML 强制作为附件且禁止脚本执行。"""
        try:
            path, row = service.artifact(artifact, current_user(request).id, conversation)
            return FileResponse(path, filename=row['filename'], media_type=row['media_type'],
                headers={'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': 'sandbox', 'Cache-Control': 'no-store'})
        except (ValueError, LookupError) as error:
            raise HTTPException(404, str(error)) from None

    @router.get('/api/conversations/{conversation}/skill-tasks/{task}/preview/{port}/')
    def preview(conversation: UUID, task: UUID, port: int, request: Request) -> RedirectResponse:
        """登录授权后发放一小时预览令牌，预览页在不透明源沙箱中执行。"""
        try:
            service.get(task, current_user(request).id, conversation)
            if not 1024 <= port <= 65535:
                raise ValueError('预览端口无效。')
            runtime.sandbox.proxy(task, port, 'GET', '/api/health', b'', 'application/json')
        except (ValueError, LookupError) as error:
            raise HTTPException(404, str(error)) from None
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with service.database.connect() as connection:
            connection.execute('DELETE FROM skill_previews WHERE expires_at<CURRENT_TIMESTAMP')
            connection.execute('INSERT INTO skill_previews VALUES(%s,%s,%s,%s)', (digest, task, port,
                datetime.now(timezone.utc) + timedelta(hours=1)))
        return RedirectResponse(f'/skill-previews/{token}/', status_code=303,
            headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})

    @router.api_route('/skill-previews/{token}/{resource:path}', methods=['GET', 'POST', 'DELETE', 'OPTIONS'])
    async def proxy(token: str, resource: str, request: Request) -> Response:
        """限定令牌、账号有效性及容器回环目标，再转发上游原生编辑器。"""
        import asyncio
        digest = hashlib.sha256(token.encode()).hexdigest()
        with service.database.connect() as connection:
            row = connection.execute("SELECT p.*,t.account_id,t.conversation_id FROM skill_previews p JOIN skill_tasks t ON t.id=p.task_id JOIN users u ON u.id=t.account_id WHERE p.token_digest=%s AND p.expires_at>CURRENT_TIMESTAMP AND u.status='active'", (digest,)).fetchone()
        if row is None:
            raise HTTPException(404, '预览已到期或不可访问。')
        try:
            service.get(row['task_id'], row['account_id'], row['conversation_id'])
        except (ValueError, LookupError):
            raise HTTPException(404, '预览资料已失效。') from None
        headers = {'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type', 'Cache-Control': 'no-store',
            'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': "sandbox allow-scripts allow-forms; default-src 'self' data: blob:; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data: blob:; frame-src 'none'; object-src 'none'"}
        if request.method == 'OPTIONS':
            return Response(headers=headers)
        if resource.startswith('/') or '..' in Path(resource).parts or '\\' in resource:
            raise HTTPException(404, '预览路径无效。')
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 2_000_000:
                raise HTTPException(413, '预览修改内容过大。')
        path = '/' + resource
        if request.url.query:
            path += '?' + request.url.query
        try:
            result = await asyncio.to_thread(runtime.sandbox.proxy, row['task_id'], row['port'], request.method,
                path, bytes(body), request.headers.get('content-type', 'application/json'))
        except ValueError:
            raise HTTPException(503, '预览已停止，请回到会话重新打开。') from None
        data = base64.b64decode(result['body'])
        mime = str(result['content_type'])
        prefix = f'/skill-previews/{token}/'
        if any(kind in mime for kind in ('text/html', 'javascript', 'text/css', 'image/svg+xml')):
            text = data.decode('utf-8')
            for source in ('/api/', '/static/', '/images/'):
                text = text.replace('"' + source, '"' + prefix + source[1:]).replace("'" + source, "'" + prefix + source[1:])
            data = text.encode()
        return Response(data, status_code=result['status'], media_type=mime, headers=headers)

    @router.get('/api/settings/skill-providers')
    def provider_status(request: Request) -> JSONResponse:
        """管理端只读取已配置变量名，不回显任何提供商变量值。"""
        require_admin(request)
        return JSONResponse({'configured': sorted(runtime.environment()), 'skills': service.catalog.entries()})

    @router.put('/api/settings/skill-providers')
    def providers(body: ProviderInput, request: Request) -> JSONResponse:
        """校验媒体提供商变量并以仅当前用户可读权限原子保存，不继承应用环境。"""
        require_admin(request)
        pattern = r'(?:IMAGE|OPENAI|GEMINI|MINIMAX|QWEN|ZHIPU|VOLCENGINE|ARK|FAL|RUNAPI|TTS|AZURE|ELEVENLABS|EDGE|VOICE|MATH|LATEX|REPLICATE|PEXELS|PIXABAY|UNSPLASH|DASHSCOPE|GOOGLE|SEEDREAM|KLING|HUNYUAN|BAIDU|SILICONFLOW|RECRAFT|TOGETHER|COMFYUI|FLUX|COSYVOICE|STABILITY|BFL|IDEOGRAM|OPENROUTER|MODELSCOPE)_[A-Z0-9_]+'
        if any(not re.fullmatch(pattern, key) or len(value) > 8192 or '\x00' in value for key, value in body.variables.items()):
            raise HTTPException(422, '仅支持图片、语音等提供商变量，不能设置应用或系统环境。')
        with runtime.lock:
            values = runtime.environment()
            values.update({key: value for key, value in body.variables.items() if value})
            for key in body.remove:
                values.pop(key, None)
            path = service.directory.parent / 'skill-providers.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix('.tmp')
            temporary.touch(mode=0o600, exist_ok=True)
            temporary.chmod(0o600)
            temporary.write_text(json.dumps(values))
            temporary.replace(path)
        return provider_status(request)

    return router
