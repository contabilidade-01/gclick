"""Cliente uazapi — POST /send/media (documento)."""

from __future__ import annotations

import httpx

from . import config


class UazapiNaoConfigurado(RuntimeError):
    pass


class UazapiTokenInvalido(RuntimeError):
    """uazapi retornou 401 — token rotacionado/revogado ou instância desconectada."""


def _base_url() -> str:
    sub, _ = config.uazapi_credentials()
    return f"https://{sub}.uazapi.com"


def _headers() -> dict:
    _, tok = config.uazapi_credentials()
    return {"token": tok, "Content-Type": "application/json"}


def enviar_documento(*, numero: str, file_url: str, doc_name: str,
                     caption: str | None = None,
                     delay_ms: int | None = None) -> dict:
    """Envia um documento (PDF) pelo WhatsApp via uazapi.

    `numero` formato: 5511999999999 (DDI+DDD+número, só dígitos).
    `file_url` aceita URL https (a do G-Click S3 é assinada e funciona) ou base64.
    `delay_ms` (opcional): a uazapi mostra "digitando..." no celular do cliente
    durante esse intervalo antes de entregar a mensagem (ms). 0/None = sem delay.
    """
    if not config.uazapi_configurado():
        raise UazapiNaoConfigurado(
            "UAZAPI_SUBDOMAIN/UAZAPI_TOKEN não configurados no .env"
        )

    payload = {
        "number": numero,
        "type": "document",
        "file": file_url,
        "docName": doc_name,
    }
    if caption:
        payload["text"] = caption
    if delay_ms and delay_ms > 0:
        # Documentado na uazapi: "Atraso em milissegundos antes do envio.
        # Durante o atraso aparecerá 'Digitando...' ou 'Gravando áudio...'"
        payload["delay"] = int(delay_ms)

    r = httpx.post(f"{_base_url()}/send/media",
                   headers=_headers(), json=payload, timeout=60)
    if r.status_code == 401:
        raise UazapiTokenInvalido(
            "Token uazapi inválido ou instância desconectada. "
            "Verifique o painel: https://free.uazapi.com (status connected? token atualizado?)."
        )
    r.raise_for_status()
    return r.json()


def enviar_texto(*, numero: str, texto: str,
                 link_url: str | None = None,
                 link_title: str | None = None,
                 link_description: str | None = None,
                 delay_ms: int | None = None) -> dict:
    """Envia mensagem de texto com hyperlink (linkPreview) pelo WhatsApp.

    `numero` formato: 5511999999999 (DDI+DDD+número, só dígitos).
    `texto` é a mensagem com o link embedado (ex: "Segue sua guia: https://...").
    `link_url` força o preview para uma URL específica (se None, usa a URL do texto).
    `link_title` personaliza o título do preview.
    `link_description` personaliza a descrição do preview.
    `delay_ms`: a uazapi mostra "digitando..." antes de entregar.
    """
    if not config.uazapi_configurado():
        raise UazapiNaoConfigurado(
            "UAZAPI_SUBDOMAIN/UAZAPI_TOKEN não configurados no .env"
        )

    payload: dict = {
        "number": numero,
        "text": texto,
    }

    if link_url or link_title or link_description:
        payload["linkPreview"] = True
        if link_url:
            payload["linkUrl"] = link_url
        if link_title:
            payload["linkPreviewTitle"] = link_title
        if link_description:
            payload["linkPreviewDescription"] = link_description

    if delay_ms and delay_ms > 0:
        payload["delay"] = int(delay_ms)

    r = httpx.post(f"{_base_url()}/send/text",
                   headers=_headers(), json=payload, timeout=60)
    if r.status_code == 401:
        raise UazapiTokenInvalido(
            "Token uazapi inválido ou instância desconectada."
        )
    r.raise_for_status()
    return r.json()


def enviar_botao_url(*, numero: str, texto: str, botao_texto: str, url: str,
                     footer: str | None = None, delay_ms: int | None = None) -> dict:
    """Envia mensagem com um BOTÃO de URL clicável (uazapi /send/menu type=button).

    Resolve o problema do WhatsApp não ter hyperlink HTML: em vez de despejar a
    URL no texto (ou usar encurtador de terceiro), manda um botão "Abrir
    documento" que abre a `url` direto. A URL fica escondida atrás do texto do
    botão — sem poluir a mensagem.

    Formato do choice de URL (spec uazapi): "texto|url:https://...".
    Usamos UM ÚNICO botão de URL (não misturar com botões de resposta — a spec
    avisa que misturar dá o aviso "abra no celular" no WhatsApp Web).
    """
    if not config.uazapi_configurado():
        raise UazapiNaoConfigurado(
            "UAZAPI_SUBDOMAIN/UAZAPI_TOKEN não configurados no .env"
        )

    payload: dict = {
        "number": numero,
        "type": "button",
        "text": texto,
        "choices": [f"{botao_texto}|url:{url}"],
    }
    if footer:
        payload["footerText"] = footer
    if delay_ms and delay_ms > 0:
        payload["delay"] = int(delay_ms)

    r = httpx.post(f"{_base_url()}/send/menu",
                   headers=_headers(), json=payload, timeout=60)
    if r.status_code == 401:
        raise UazapiTokenInvalido(
            "Token uazapi inválido ou instância desconectada."
        )
    r.raise_for_status()
    return r.json()


def consultar_fila() -> dict:
    """Estado da fila interna de envio assíncrono da uazapi.

    `GET /message/async` — devolve dict compatível com `testar_conexao()`:
      ok: bool — chamada bem sucedida
      categoria: "ok" | "nao_configurado" | "token_invalido" | "rede"
      mensagem: str
      detalhes: { status, pending, processingNow, ... } — campos da uazapi
    """
    if not config.uazapi_configurado():
        return {"ok": False, "categoria": "nao_configurado",
                "mensagem": "uazapi não configurada.",
                "detalhes": {}}
    try:
        r = httpx.get(f"{_base_url()}/message/async",
                      headers=_headers(), timeout=15)
    except httpx.RequestError as e:
        return {"ok": False, "categoria": "rede",
                "mensagem": f"Falha de rede: {e}", "detalhes": {}}

    if r.status_code == 401:
        return {"ok": False, "categoria": "token_invalido",
                "mensagem": "Token uazapi rejeitado.",
                "detalhes": {"http": 401}}
    if r.status_code >= 500:
        return {"ok": False, "categoria": "rede",
                "mensagem": f"uazapi devolveu HTTP {r.status_code}.",
                "detalhes": {"http": r.status_code}}
    try:
        j = r.json() or {}
    except Exception:
        return {"ok": False, "categoria": "rede",
                "mensagem": "Resposta da uazapi não é JSON.",
                "detalhes": {}}
    status = j.get("status") or "desconhecido"
    return {"ok": True, "categoria": "ok",
            "mensagem": f"Fila uazapi: {status}.",
            "detalhes": j}


def limpar_fila() -> dict:
    """Apaga todas as mensagens pendentes na fila async da uazapi.
    `DELETE /message/async` — útil como reset de emergência.
    """
    if not config.uazapi_configurado():
        return {"ok": False, "categoria": "nao_configurado",
                "mensagem": "uazapi não configurada.", "detalhes": {}}
    try:
        r = httpx.delete(f"{_base_url()}/message/async",
                         headers=_headers(), timeout=30)
    except httpx.RequestError as e:
        return {"ok": False, "categoria": "rede",
                "mensagem": f"Falha de rede: {e}", "detalhes": {}}
    if r.status_code == 401:
        return {"ok": False, "categoria": "token_invalido",
                "mensagem": "Token uazapi rejeitado.", "detalhes": {"http": 401}}
    if r.status_code >= 400:
        return {"ok": False, "categoria": "rede",
                "mensagem": f"uazapi devolveu HTTP {r.status_code}.",
                "detalhes": {"http": r.status_code}}
    try:
        j = r.json() or {}
    except Exception:
        j = {}
    return {"ok": True, "categoria": "ok",
            "mensagem": "Fila uazapi limpa.", "detalhes": j}


def testar_conexao() -> dict:
    """Diagnóstico — chama /instance/status e devolve estado estruturado.

    Retorna um dict com:
      ok: bool — se a conexão está saudável (token válido E instância connected)
      categoria: "ok" | "nao_configurado" | "token_invalido" | "desconectado" | "rede"
      mensagem: str humana
      detalhes: dict — campos brutos relevantes (status, owner, profileName, etc.)
    """
    if not config.uazapi_configurado():
        return {"ok": False, "categoria": "nao_configurado",
                "mensagem": "UAZAPI_SUBDOMAIN/UAZAPI_TOKEN ausentes no .env.",
                "detalhes": {}}

    try:
        r = httpx.get(f"{_base_url()}/instance/status",
                      headers=_headers(), timeout=15)
    except httpx.RequestError as e:
        return {"ok": False, "categoria": "rede",
                "mensagem": f"Falha de rede: {e}", "detalhes": {}}

    if r.status_code == 401:
        return {"ok": False, "categoria": "token_invalido",
                "mensagem": "Token rejeitado pela uazapi (401 Invalid token). "
                            "Pegue um novo no painel e atualize o .env.",
                "detalhes": {"http": 401}}
    if r.status_code >= 500:
        return {"ok": False, "categoria": "rede",
                "mensagem": f"uazapi devolveu HTTP {r.status_code}. Tente de novo em alguns minutos.",
                "detalhes": {"http": r.status_code}}

    try:
        j = r.json()
    except Exception:
        return {"ok": False, "categoria": "rede",
                "mensagem": "Resposta da uazapi não é JSON.", "detalhes": {}}

    inst = j.get("instance") or {}
    status = inst.get("status") or "desconhecido"
    if status != "connected":
        return {"ok": False, "categoria": "desconectado",
                "mensagem": f"Instância está '{status}' (esperado 'connected'). "
                            f"Abra o painel e re-escaneie o QR code.",
                "detalhes": {"status": status, "owner": inst.get("owner"),
                             "profileName": inst.get("profileName")}}
    return {"ok": True, "categoria": "ok",
            "mensagem": f"Conectado ✅ (número {inst.get('owner')} · "
                        f"perfil {inst.get('profileName')})",
            "detalhes": {"status": status, "owner": inst.get("owner"),
                         "profileName": inst.get("profileName")}}
