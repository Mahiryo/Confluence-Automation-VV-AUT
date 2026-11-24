import csv
from datetime import datetime
import math
from pathlib import Path
import sys
import time
from urllib.parse import quote, urljoin, urlsplit

import crawler as c

#import pymongo

#myclient = pymongo.MongoClient("mongodb://localhost:27017/")
# mydb = myclient["mydatabase"]
# mycol = mydb["customers"]

# mydict = { "name": "John", "address": "Highway 37" }


PASTA_MONITOR = "monitor"
CSV_SAIDA = "paginas_alteradas.csv"

CICLO_HORAS = 24
CICLO_SEGUNDOS = CICLO_HORAS * 3600

LOTE = 200
LIMITE_URL = 6000
JANELA_INICIAL_H = 24
MARGEM_H = 1
MAX_PAGINAS_RESULTADO = 50
ESPERA_CSV_VAZIO = 60

CAMPOS_CSV = [
    "VERIFICADO_EM",
    "PAGE_ID",
    "TITULO",
    "VERSAO",
    "ALTERADO_EM",
    "AUTOR"
]


def carregar_ids():
    return list(dict.fromkeys(
        str(linha["id"])
        for linha in c.ler_csv(c.CSV_ENTRADA)
        if linha["situacao"] == "valida"
    ))


def criar_lotes(ids):
    return [
        ids[inicio:inicio + LOTE]
        for inicio in range(0, len(ids), LOTE)
    ]


def calcular_intervalo(lotes):
    return CICLO_SEGUNDOS / max(1, len(lotes))


def calcular_janela(lote, estado, agora):
    verificacoes = estado.get("verificacoes", {})
    versoes = estado.get("versoes", {})
    ultima_execucao = estado.get("ultima_execucao")

    janelas = []

    for page_id in lote:
        ultima = verificacoes.get(page_id)

        if ultima is None and page_id in versoes:
            ultima = ultima_execucao

        if ultima is None:
            janelas.append(JANELA_INICIAL_H)
            continue

        horas = math.ceil(
            max(0, agora - ultima) / 3600
        ) + MARGEM_H

        janelas.append(horas)

    return max(janelas, default=JANELA_INICIAL_H)


def esperar_ate(instante):
    espera = max(0, instante - time.monotonic())

    if espera > 0:
        c.log.info(
            "Próxima execução em %.2f minutos.",
            espera / 60
        )

        time.sleep(espera)


def buscar_alteradas(sessao, base_url, lote, horas):
    cql = quote(
        f'id in ({",".join(lote)}) '
        f'and lastmodified >= now("-{horas}h")',
        safe=""
    )

    url = (
        f"{base_url}/rest/api/content/search"
        f"?cql={cql}&expand=version&limit={len(lote)}"
    )

    if len(url) > LIMITE_URL:
        if len(lote) <= 1:
            raise LookupError("Limite de URL excedido.")

        meio = len(lote) // 2

        return (
            buscar_alteradas(
                sessao, base_url, lote[:meio], horas
            )
            +
            buscar_alteradas(
                sessao, base_url, lote[meio:], horas
            )
        )

    encontradas = []
    visitadas = set()

    origem = urlsplit(base_url)

    for _ in range(MAX_PAGINAS_RESULTADO):
        if url in visitadas:
            raise LookupError("Loop de paginação detectado.")

        visitadas.add(url)

        destino = urlsplit(url)

        if (
            destino.scheme != origem.scheme
            or destino.netloc != origem.netloc
        ):
            raise LookupError(
                "Endereço externo detectado na paginação."
            )

        with c.pedir(sessao, url) as resposta:
            if (
                resposta.status_code == 401
                or c.eh_login(resposta.url)
            ):
                raise PermissionError("PAT recusado.")

            if resposta.status_code != 200:
                raise LookupError(
                    f"HTTP {resposta.status_code}"
                )

            try:
                dados = resposta.json()
            except ValueError:
                raise LookupError(
                    "Resposta inválida da API."
                ) from None

        encontradas.extend(dados.get("results", []))

        proxima = (
            dados.get("_links") or {}
        ).get("next")

        if not proxima:
            return encontradas

        if proxima.startswith("/rest/"):
            url = f"{base_url}{proxima}"
        else:
            url = urljoin(url, proxima)

    raise LookupError(
        "Limite de paginação excedido."
    )


def identificar_alteracoes(itens, lote, versoes):
    novas = []
    atualizadas = versoes.copy()
    permitidos = set(lote)

    for item in itens:
        page_id = str(item.get("id"))

        if page_id not in permitidos:
            continue

        versao = item.get("version") or {}
        numero = int(versao.get("number") or 0)

        if numero <= atualizadas.get(page_id, 0):
            continue

        atualizadas[page_id] = numero

        novas.append({
            "VERIFICADO_EM": datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "PAGE_ID": page_id,
            "TITULO": item.get("title", ""),
            "VERSAO": numero,
            "ALTERADO_EM": versao.get("when", ""),
            "AUTOR": (
                versao.get("by") or {}
            ).get("displayName", "")
        })

    return novas, atualizadas


def salvar_alteracoes(pasta, novas):
    if not novas:
        return

    caminho = pasta / CSV_SAIDA

    novo_arquivo = (
        not caminho.exists()
        or caminho.stat().st_size == 0
    )

    encoding = "utf-8-sig" if novo_arquivo else "utf-8"

    with open(
        caminho,
        "a",
        encoding=encoding,
        newline=""
    ) as arquivo:

        escritor = csv.DictWriter(
            arquivo,
            fieldnames=CAMPOS_CSV,
            delimiter=";"
        )

        if novo_arquivo:
            escritor.writeheader()

        escritor.writerows(novas)

    for linha in novas:
        c.log.info(
            "%s v%s: %s",
            linha["PAGE_ID"],
            linha["VERSAO"],
            linha["TITULO"]
        )


def processar_lote(
    sessao,
    base_url,
    lote,
    pasta,
    estado,
    estado_arquivo
):
    inicio = time.time()

    horas = calcular_janela(
        lote,
        estado,
        inicio
    )

    itens = buscar_alteradas(
        sessao,
        base_url,
        lote,
        horas
    )

    novas, versoes = identificar_alteracoes(
        itens,
        lote,
        estado.get("versoes", {})
    )

    salvar_alteracoes(pasta, novas)

    verificacoes = estado.setdefault(
        "verificacoes", {}
    )

    for page_id in lote:
        verificacoes[page_id] = inicio

    estado["versoes"] = versoes

    c.gravar_estado(
        estado_arquivo,
        estado
    )

    return len(novas)


def executar_ciclo(
    sessao,
    base_url,
    pasta,
    estado,
    estado_arquivo
):
    ids = carregar_ids()

    if not ids:
        c.log.warning("Nenhum Page ID válido.")

        time.sleep(ESPERA_CSV_VAZIO)

        return

    verificacoes = estado.get("verificacoes", {})

    ids.sort(
        key=lambda page_id: verificacoes.get(page_id, 0)
    )

    lotes = criar_lotes(ids)
    intervalo = calcular_intervalo(lotes)

    inicio_ciclo = time.monotonic()
    fim_ciclo = inicio_ciclo + CICLO_SEGUNDOS

    alteradas = 0
    verificadas = 0
    falhas = 0

    requisicoes_iniciais = c.requisicoes

    c.log.info(
        "=== Novo ciclo de %s horas ===",
        CICLO_HORAS
    )

    c.log.info("Page IDs: %s", len(ids))
    c.log.info("Lotes: %s", len(lotes))
    c.log.info("Intervalo: %.2f min", intervalo / 60)

    for indice, lote in enumerate(lotes):
        horario = inicio_ciclo + indice * intervalo

        esperar_ate(horario)

        c.log.info(
            "Lote %s/%s | %s Page IDs",
            indice + 1,
            len(lotes),
            len(lote)
        )

        try:
            alteradas += processar_lote(
                sessao,
                base_url,
                lote,
                pasta,
                estado,
                estado_arquivo
            )

            verificadas += len(lote)

        except PermissionError:
            raise

        except (
            LookupError,
            c.requests.RequestException,
            RuntimeError
        ) as exc:
            falhas += len(lote)

            c.log.error(
                "Falha no lote %s: %s",
                indice + 1,
                exc
            )

        progresso = verificadas / len(ids) * 100

        c.log.info(
            "Progresso: %s/%s | %.2f%%",
            verificadas,
            len(ids),
            progresso
        )

    c.log.info("=== Consultas do ciclo encerradas ===")
    c.log.info("Verificadas: %s", verificadas)
    c.log.info("Alteradas: %s", alteradas)
    c.log.info("Falhas: %s", falhas)

    c.log.info(
        "Requisições HTTP: %s",
        c.requisicoes - requisicoes_iniciais
    )

    esperar_ate(fim_ciclo)


def main():
    pasta = Path(PASTA_MONITOR)
    pasta.mkdir(parents=True, exist_ok=True)

    c.configurar_log(pasta)

    estado_arquivo = pasta / "estado_monitor.json"
    estado = c.ler_estado(estado_arquivo)

    base_url, pat, ca_bundle = c.ler_env()
    base_url = base_url.rstrip("/")

    try:
        with c.criar_sessao(pat, ca_bundle) as sessao:
            sessao.headers["Accept"] = "application/json"

            while True:
                executar_ciclo(
                    sessao,
                    base_url,
                    pasta,
                    estado,
                    estado_arquivo
                )

    except KeyboardInterrupt:
        c.log.info("Monitor encerrado pelo usuário.")

        return 0

    except PermissionError as exc:
        c.log.error("Falha de autenticação: %s", exc)

        return 1


if __name__ == "__main__":
    sys.exit(main())