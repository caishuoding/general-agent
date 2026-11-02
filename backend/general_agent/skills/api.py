"""提供无状态 Skill 内容、生成文件和隔离预览接口。"""

import asyncio
import base64
import hashlib
import json
import re
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from general_agent.auth.dependencies import current_user, require_admin
from general_agent.skills.runtime import SkillRuntime


class ProviderInput(BaseModel):
    """仅允许可选媒体提供商变量，空字符串保留原配置，删除必须明确列出。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    variables: dict[str, str] = Field(default_factory=dict)
    remove: list[str] = Field(default_factory=list)


def skill_router(runtime: SkillRuntime) -> APIRouter:
    """组装 Skill 正文、所属会话文件和按短期能力令牌授权的隔离预览。"""
    router = APIRouter()
    service = runtime.service

    @router.get('/api/skills')
    def catalog(request: Request) -> JSONResponse:
        """登录用户可查看可用 Skill 及完整描述，不执行任何制作工作。"""
        current_user(request)
        return JSONResponse(service.catalog.entries())

    @router.get('/api/skills/{name}/content')
    def skill_content(name: str, request: Request) -> JSONResponse:
        """返回已安装 Skill 的完整入口，供显式选择在前端构造用户消息。"""
        current_user(request)
        try:
            item = service.catalog.read(name, 'SKILL.md')
        except ValueError as error:
            raise HTTPException(404, str(error)) from None
        return JSONResponse({
            'name': item['name'],
            'version': item['version'],
            'path': item['path'],
            'content': item['content'],
            'digest': item['file_digest'],
        })

    @router.get('/api/conversations/{conversation}/generated-files')
    def files(conversation: UUID, request: Request) -> JSONResponse:
        """读取当前账号会话中真实发布的生成文件，不返回内部工作路径。"""
        try:
            rows = service.list_files(current_user(request).id, conversation)
            return JSONResponse(jsonable_encoder(rows))
        except LookupError as error:
            raise HTTPException(404, str(error)) from None

    @router.get('/api/conversations/{conversation}/generated-files/{file_id}/file')
    def download(conversation: UUID, file_id: UUID, request: Request) -> FileResponse:
        """重新授权后下载不可变生成文件，危险内容强制作为附件。"""
        try:
            path, row = service.artifact(file_id, current_user(request).id, conversation)
            return FileResponse(path, filename=row['filename'], media_type=row['media_type'],
                                headers={'X-Content-Type-Options': 'nosniff',
                                         'Content-Security-Policy': 'sandbox',
                                         'Cache-Control': 'no-store'})
        except (ValueError, LookupError) as error:
            raise HTTPException(404, str(error)) from None

    @router.api_route('/skill-previews/{token}/{resource:path}',
                      methods=['GET', 'POST', 'DELETE', 'OPTIONS'])
    async def proxy(token: str, resource: str, request: Request) -> Response:
        """限定预览令牌、有效账号和 Run 容器回环端口后转发上游页面。"""
        digest = hashlib.sha256(token.encode()).hexdigest()
        row = service.preview(digest)
        if row is None or row['user_status'] != 'active':
            raise HTTPException(404, '预览已到期或不可访问。')
        try:
            service.run(row['account_id'], row['conversation_id'], row['run_id'])
        except (ValueError, LookupError):
            raise HTTPException(404, '预览资料已失效。') from None
        headers = {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type',
            'Cache-Control': 'no-store',
            'Referrer-Policy': 'no-referrer',
            'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': "sandbox allow-scripts allow-forms; default-src 'self' data: blob:; "
                                       "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                                       "connect-src 'self'; img-src 'self' data: blob:; frame-src 'none'; object-src 'none'",
        }
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
            result = await asyncio.to_thread(runtime.sandbox.proxy, row['run_id'], row['port'],
                                             request.method, path, bytes(body),
                                             request.headers.get('content-type', 'application/json'))
        except ValueError:
            raise HTTPException(503, '预览已停止，请回到会话重新打开。') from None
        data = base64.b64decode(result['body'])
        mime = str(result['content_type'])
        prefix = f'/skill-previews/{token}/'
        if any(kind in mime for kind in ('text/html', 'javascript', 'text/css', 'image/svg+xml')):
            text = data.decode('utf-8')
            for source in ('/api/', '/static/', '/images/'):
                text = text.replace('"' + source, '"' + prefix + source[1:])
                text = text.replace("'" + source, "'" + prefix + source[1:])
            data = text.encode()
        return Response(data, status_code=result['status'], media_type=mime, headers=headers)

    @router.get('/api/settings/skill-providers')
    def provider_status(request: Request) -> JSONResponse:
        """管理端只读取已配置变量名，不回显任何提供商变量值。"""
        require_admin(request)
        return JSONResponse({'configured': sorted(runtime.environment()), 'skills': service.catalog.entries()})

    @router.put('/api/settings/skill-providers')
    def providers(body: ProviderInput, request: Request) -> JSONResponse:
        """校验媒体提供商变量并以仅当前用户可读权限原子保存。"""
        require_admin(request)
        pattern = (r'(?:IMAGE|OPENAI|GEMINI|MINIMAX|QWEN|ZHIPU|VOLCENGINE|ARK|FAL|RUNAPI|TTS|AZURE|'
                   r'ELEVENLABS|EDGE|VOICE|MATH|LATEX|REPLICATE|PEXELS|PIXABAY|UNSPLASH|DASHSCOPE|'
                   r'GOOGLE|SEEDREAM|KLING|HUNYUAN|TENCENT|TOKENHUB|LAS|BAIDU|SILICONFLOW|RECRAFT|'
                   r'TOGETHER|COMFYUI|FLUX|COSYVOICE|STABILITY|BFL|IDEOGRAM|OPENROUTER|MODELSCOPE)_[A-Z0-9_]+')
        if any(not re.fullmatch(pattern, key) or len(value) > 8192 or '\x00' in value
               for key, value in body.variables.items()):
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
