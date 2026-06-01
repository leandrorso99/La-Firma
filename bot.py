from __future__ import annotations

import asyncio
import json
import logging
import socket
from typing import Any

import aiohttp

import config

logger = logging.getLogger(__name__)


class FinanceApiError(Exception):
    pass


class FinanceApiClient:
    def __init__(self) -> None:
        self.base_url = config.FINANCE_API_BASE_URL.rstrip('/')
        self.token = config.FINANCE_API_TOKEN
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        """Retorna uma sessão reutilizável. Cria uma nova se necessário."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            connector = aiohttp.TCPConnector(
                limit=10,
                limit_per_host=5,
                ttl_dns_cache=300,
                enable_cleanup_closed=True,
            )
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                headers={
                    'Content-Type': 'application/json',
                    'X-Finance-Token': self.token,
                },
            )
        return self._session

    async def close(self) -> None:
        """Fecha a sessão HTTP. Chame no cog_unload."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _request(self, method: str, endpoint: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f'{self.base_url}/{endpoint.lstrip("/")}'
        max_retries = 3

        for attempt in range(1, max_retries + 1):
            try:
                session = self._get_session()
                async with session.request(method, url, json=payload) as response:
                    text = await response.text()
                    content_type = response.headers.get('Content-Type', '')

                    try:
                        data = json.loads(text) if text.strip() else None
                    except json.JSONDecodeError:
                        preview = text.strip().replace('\n', ' ')[:300]
                        raise FinanceApiError(
                            f'Resposta não JSON da API ({response.status}, {content_type}): {preview or "vazia"}'
                        )

                    if not isinstance(data, dict):
                        preview = text.strip().replace('\n', ' ')[:300]
                        raise FinanceApiError(
                            f'Resposta inesperada da API ({response.status}, {content_type}): {preview or "vazia"}'
                        )

                    if response.status >= 400 or data.get('success') is False:
                        detail = data.get('error') or text.strip()[:300] or f'HTTP {response.status}'
                        raise FinanceApiError(f'Erro da API ({response.status}): {detail}')

                    return data
            except asyncio.TimeoutError as exc:
                last_exc = FinanceApiError('A API demorou demais para responder. Tente novamente em instantes.')
                last_exc.__cause__ = exc
            except aiohttp.ServerDisconnectedError as exc:
                last_exc = FinanceApiError(f'O servidor desconectou durante a requisição.')
                last_exc.__cause__ = exc
            except aiohttp.ClientConnectorDNSError as exc:
                host = getattr(getattr(exc, 'host', None), 'strip', lambda: '')()
                if host == '':
                    host = self._extract_host_from_url(url)
                raise FinanceApiError(
                    f'Falha de DNS ao resolver {host or "o host da API"}. Verifique a internet, o DNS e se o domínio está acessível nessa máquina.'
                ) from exc
            except aiohttp.ClientError as exc:
                os_error = getattr(exc, 'os_error', None)
                if isinstance(os_error, socket.gaierror):
                    host = self._extract_host_from_url(url)
                    raise FinanceApiError(
                        f'Falha de DNS ao resolver {host or "o host da API"}. Verifique a internet, o DNS e se o domínio está acessível nessa máquina.'
                    ) from exc
                last_exc = FinanceApiError(f'Não foi possível conectar na API: {exc}')
                last_exc.__cause__ = exc
            except FinanceApiError:
                raise

            # Retry com backoff curto para erros de conexão/timeout
            if attempt < max_retries:
                wait = attempt * 2
                logger.info('Tentativa %d/%d falhou para %s %s, retentando em %ds...', attempt, max_retries, method, endpoint, wait)
                # Recria sessão caso a conexão tenha sido corrompida
                await self.close()
                await asyncio.sleep(wait)
            else:
                logger.warning('Todas as %d tentativas falharam para %s %s.', max_retries, method, endpoint)
                raise last_exc

    @staticmethod
    def _extract_host_from_url(url: str) -> str:
        cleaned = url.split('://', 1)[-1]
        return cleaned.split('/', 1)[0].strip()

    async def post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request('POST', endpoint, payload)

    async def get(self, endpoint: str) -> dict[str, Any]:
        return await self._request('GET', endpoint)
