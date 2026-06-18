"""Check de enviados — matriz cliente × tipo de documento na competência."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .. import auth, db, gclick, helpers, tipos as tipos_mod
from ..templating import templates

router = APIRouter()


@router.get("/check", response_class=HTMLResponse)
def check(request: Request, competencia: str | None = None):
    # `def`: consulta o G-Click — roda no threadpool, não trava o app.
    if redir := auth.requer_login(request):
        return redir
    usuario = auth.usuario_da_requisicao(request)

    hoje = date.today()
    competencia = competencia or f"{hoje.year:04d}-{hoje.month:02d}"

    tipos = db.listar_tipos(ativos_apenas=True)
    enviadas_keys = db.chaves_enviadas()
    whatsapp_map = db.map_whatsapp_por_cnpj()

    # cnpj -> {apelido, celulas:{codigo: {status, titulo}}, tem_whatsapp}
    matriz_por_cnpj: dict[str, dict] = {}
    nao_classificadas: list[str] = []
    erro_carga: str | None = None
    stats = {"clientes": 0, "enviadas": 0, "prontas": 0, "sem_anexo": 0}

    def _celula_default() -> dict:
        return {codigo: {"status": "vazio", "titulo": ""} for codigo in (t["codigo"] for t in tipos)}

    divergencias: list[dict] = []
    try:
        dados = helpers.carregar_tarefas_e_ativs(competencia, None)
        for t, ativs in dados:
            cnpj = t.get("clienteInscricao") or ""
            apelido = t.get("clienteApelido") or cnpj
            for g in gclick.extrair_guias_pendentes(t, ativs):
                cls = tipos_mod.classificar(g["atividade_nome"], g["obrigacao_nome"])
                if not cls:
                    nao_classificadas.append(f"{g['obrigacao_nome']} / {g['atividade_nome']}")
                    continue
                codigo, _ = cls
                # Detector de divergência: nome do arquivo casa com tipo diferente
                if g.get("arquivo_nome"):
                    cls_arq = tipos_mod.classificar(g["arquivo_nome"])
                    if cls_arq and cls_arq[0] != codigo:
                        divergencias.append({
                            "cnpj": cnpj, "apelido": apelido,
                            "atividade_nome": g["atividade_nome"],
                            "arquivo_nome": g["arquivo_nome"],
                            "tipo_atividade": codigo,
                            "tipo_arquivo": cls_arq[0],
                        })
                linha = matriz_por_cnpj.setdefault(cnpj, {
                    "cnpj": cnpj, "apelido": apelido,
                    "celulas": _celula_default(),
                    "tem_whatsapp": cnpj in whatsapp_map,
                })
                ja = (cnpj, g["tarefa_id"], g["atividade_id"]) in enviadas_keys
                tem_pdf = bool(g["arquivo_url"])
                if ja:
                    status, titulo = "enviado", f"Enviada — {g['arquivo_nome']}"
                elif tem_pdf:
                    status, titulo = "pronto", f"Pronta — {g['arquivo_nome']}"
                else:
                    status, titulo = "sem_anexo", "Sem anexo no G-Click ainda"
                # Se já enviado, prevalece sobre pronto. Se pronto, prevalece sobre sem_anexo.
                atual = linha["celulas"][codigo]["status"]
                priority = {"vazio": 0, "sem_anexo": 1, "pronto": 2, "enviado": 3}
                if priority[status] > priority[atual]:
                    linha["celulas"][codigo] = {"status": status, "titulo": titulo}
        # contagem
        for linha in matriz_por_cnpj.values():
            for cel in linha["celulas"].values():
                if cel["status"] == "enviado":
                    stats["enviadas"] += 1
                elif cel["status"] == "pronto":
                    stats["prontas"] += 1
                elif cel["status"] == "sem_anexo":
                    stats["sem_anexo"] += 1
        stats["clientes"] = len(matriz_por_cnpj)
    except Exception as e:
        erro_carga = str(e)

    matriz = sorted(matriz_por_cnpj.values(), key=lambda x: x["apelido"] or "")

    # remove duplicatas mantendo ordem
    seen: set[str] = set()
    nao_class_uniq = []
    for s in nao_classificadas:
        if s not in seen:
            seen.add(s)
            nao_class_uniq.append(s)

    return templates.TemplateResponse(request, "check.html", {
        "request": request,
        "usuario": usuario,
        "active": "check",
        "competencia": competencia,
        "competencias_opcoes": helpers.competencias_opcoes(competencia),
        "tipos": tipos,
        "matriz": matriz,
        "stats": stats,
        "nao_classificadas": nao_class_uniq,
        "divergencias": divergencias,
        "erro_carga": erro_carga,
    })
