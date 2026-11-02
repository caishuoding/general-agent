"""为隔离任务提供仅连接公网地址的 HTTP/HTTPS 正向代理。"""

import ipaddress
import json
import os
import select
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, urlencode
from urllib.request import Request, urlopen


class PublicProxy(BaseHTTPRequestHandler):
    """拒绝私网和元数据地址，DNS 解析后直接连接已验证的 IP。"""

    def log_message(self, format: str, *args: object) -> None:
        """避免日志保存带密钥或用户资料的完整请求地址。"""
        return

    def connect_public(self, host: str, port: int) -> socket.socket:
        """校验所有解析结果并固定目标 IP，防止重解析进入私网。"""
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        resolver = os.environ.get('SKILL_EGRESS_DOH', '')
        if resolver:
            if resolver not in {'https://cloudflare-dns.com/dns-query', 'https://dns.google/resolve'}:
                raise ValueError('不支持的可信 DNS 解析器')
            query = resolver + '?' + urlencode({'name': host, 'type': 'A'})
            with urlopen(Request(query, headers={'Accept': 'application/dns-json'}), timeout=10) as response:
                answer = json.loads(response.read(65536))
            addresses = [(socket.AF_INET, socket.SOCK_STREAM, 0, '', (item['data'], port))
                for item in answer.get('Answer', []) if item.get('type') == 1]
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError('仅允许公网地址')
        if port not in {80, 443}:
            raise ValueError('仅允许 HTTP/HTTPS 标准端口')
        family, kind, protocol, _, address = addresses[0]
        stream = socket.socket(family, kind, protocol)
        stream.settimeout(30)
        stream.connect(address)
        return stream

    def relay(self, remote: socket.socket) -> None:
        """在有界空闲时间内双向转发字节，不解析或记录 TLS 内容。"""
        sockets = [self.connection, remote]
        while True:
            ready, _, _ = select.select(sockets, [], [], 60)
            if not ready:
                return
            for source in ready:
                data = source.recv(65536)
                if not data:
                    return
                (remote if source is self.connection else self.connection).sendall(data)

    def do_CONNECT(self) -> None:
        """验证 HTTPS 隧道目的地后建立透明转发。"""
        connected = False
        try:
            target = urlsplit('//' + self.path)
            with self.connect_public(target.hostname or '', target.port or 443) as remote:
                self.send_response(200, 'Connection Established')
                self.end_headers()
                connected = True
                self.wfile.flush()
                self.relay(remote)
        except (ValueError, OSError):
            if not connected:
                self.send_error(403, 'Public destination required')
            self.close_connection = True

    def do_GET(self) -> None:
        """转发有界 HTTP 请求，拒绝非公网目标和不支持的分块上传。"""
        try:
            target = urlsplit(self.path)
            if target.scheme != 'http' or target.username or target.password:
                raise ValueError('无效代理请求')
            with self.connect_public(target.hostname or '', target.port or 80) as remote:
                length = int(self.headers.get('Content-Length', '0'))
                if length < 0 or length > 50_000_000 or self.headers.get('Transfer-Encoding'):
                    raise ValueError('代理请求体过大或不支持分块上传')
                body = self.rfile.read(length)
                path = target.path or '/'
                if target.query:
                    path += '?' + target.query
                headers = ''.join(f'{key}: {value}\r\n' for key, value in self.headers.items()
                    if key.lower() not in {'proxy-connection', 'connection', 'proxy-authorization'})
                remote.sendall(f'{self.command} {path} HTTP/1.1\r\n{headers}Connection: close\r\n\r\n'.encode('latin-1') + body)
                self.relay(remote)
        except (ValueError, OSError):
            self.send_error(403, 'Public destination required')
        self.close_connection = True

    def do_HEAD(self) -> None:
        """使用同一校验规则转发只读取响应头的请求。"""
        self.do_GET()

    def do_POST(self) -> None:
        """允许 HTTP 提供商发送有界请求体，目标仍必须是已校验公网地址。"""
        self.do_GET()

    def do_DELETE(self) -> None:
        """对 HTTP 提供商复用相同的目标和请求体校验。"""
        self.do_GET()


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 3128), PublicProxy).serve_forever()
