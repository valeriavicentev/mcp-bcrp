"""Servidor MCP del BCRP. Empiecen aquí: buscar_series ya funciona; completen los TODO hasta que pasen las pruebas.

    uv run pytest                                           # el contrato: las 8 pruebas deben pasar
    uv run server.py                                        # el servidor, por stdio
    claude mcp add --scope project bcrp -- uv run server.py # conectarlo a Claude Code

Las cuentas las hace Python, no el modelo: así cada cifra sale de una llamada que se puede repetir.
"""
import re
import time
import unicodedata
from functools import cache
from pathlib import Path

import httpx
import pandas as pd
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

AQUI = Path(__file__).parent
CATALOGO = AQUI / "datos" / "catalogo_bcrp.csv"
URL_CATALOGO = "https://estadisticas.bcrp.gob.pe/estadisticas/series/metadata"
API = "https://estadisticas.bcrp.gob.pe/estadisticas/series/api"
SOLO_LECTURA = {"readOnlyHint": True, "openWorldHint": True}

mcp = FastMCP("bcrp", instructions=(
    "Series oficiales del Banco Central de Reserva del Perú (BCRP). Primero busca el código con buscar_series; "
    "luego usa resumen para variaciones, promedios y extremos, y serie para los datos. No calcules a mano: "
    "cita siempre el código de la serie y el periodo."))


def _sin_tildes(s):
    return unicodedata.normalize("NFD", str(s).lower()).encode("ascii", "ignore").decode()


@cache
def catalogo():
    """El catálogo oficial: 17 mil series en unos 7 MB. Se baja una vez y queda en datos/."""
    if not CATALOGO.exists():
        CATALOGO.parent.mkdir(exist_ok=True)
        r = httpx.get(URL_CATALOGO, timeout=120)
        r.raise_for_status()
        CATALOGO.write_text(r.content.decode("latin-1"), encoding="utf-8")
    df = pd.read_csv(CATALOGO, sep=";", dtype=str).fillna("")
    df = df.rename(columns={"Código de serie": "codigo", "Categoría de serie": "categoria", "Grupo de serie": "grupo",
                            "Nombre de serie": "nombre", "Frecuencia": "frecuencia",
                            "Fecha de actualización": "actualizada", "Fecha de inicio": "inicio"})
    df["texto"] = (df.categoria + " " + df.grupo + " " + df.nombre).map(_sin_tildes)
    return df


def _ficha(codigo):
    fila = catalogo().query("codigo == @codigo")
    if fila.empty:
        raise ToolError(f"No existe el código de serie {codigo!r}. Búsquenlo con buscar_series.")
    f = fila.iloc[0]
    return {"codigo": f.codigo, "nombre": f"{f.grupo} - {f.nombre}", "categoria": f.categoria,
            "frecuencia": f.frecuencia, "inicio": f.inicio, "actualizada": f.actualizada}


@mcp.tool(annotations=SOLO_LECTURA)
def buscar_series(texto: str, frecuencia: str | None = None, limite: int = 10) -> list[dict]:
    """Busca series del BCRP por palabras: todas deben aparecer en la categoría, el grupo o el nombre.

    Ejemplos: "ipc 12 meses", "tipo de cambio interbancario venta", "reservas internacionales netas",
    "tasa de referencia", "cobre precio". frecuencia: "Diaria", "Mensual", "Trimestral" o "Anual".
    Devuelve el código, el nombre completo, la frecuencia y desde cuándo hay datos. Si no hay resultados,
    prueben con menos palabras.
    """
    df = catalogo()
    m = pd.Series(True, index=df.index)
    for palabra in _sin_tildes(texto).split():
        m &= df.texto.str.contains(re.escape(palabra))
    if frecuencia:
        m &= df.frecuencia.str.lower() == frecuencia.lower()
    # primero las series vigentes y, entre ellas, las de nombre más corto (las más generales)
    sel = df[m]
    orden = sel.assign(vieja=sel.actualizada < "2025", largo=(sel.grupo + sel.nombre).str.len())
    return [_ficha(c) for c in orden.sort_values(["vieja", "largo"]).head(limite).codigo]


MESES = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
         "jul": 7, "ago": 8, "set": 9, "sep": 9, "oct": 10, "nov": 11, "dic": 12}
_descargas = {}


def _fecha_api(fecha, frecuencia, final=False):
    """Convierte "2025-01", "2025-T1", "2025-01-15" o "2025" al formato que pide la API según la frecuencia."""
    partes = str(fecha).strip().upper().replace("/", "-").split("-")
    anio = int(partes[0])
    frecuencia = frecuencia.lower()
    if frecuencia == "anual":
        return f"{anio}"
    if frecuencia == "trimestral":
        if len(partes) == 1:
            return f"{anio}-{4 if final else 1}"
        if partes[1].startswith("T"):
            return f"{anio}-{int(partes[1][1:])}"
        return f"{anio}-{(int(partes[1]) - 1) // 3 + 1}"   # un mes: su trimestre
    if frecuencia == "diaria":
        mes = int(partes[1]) if len(partes) > 1 else (12 if final else 1)
        if len(partes) > 2:
            dia = int(partes[2])
        else:
            dia = pd.Period(f"{anio}-{mes:02d}", "M").days_in_month if final else 1
        return f"{anio}-{mes:02d}-{dia:02d}"
    mes = int(partes[1]) if len(partes) > 1 else (12 if final else 1)   # mensual
    return f"{anio}-{mes}"


def _periodo(nombre):
    """Pasa los nombres de la API a ISO: "Ene.2025" -> "2025-01", "01.Oct.26" -> "2026-10-01",
    "T1.25" -> "2025-T1", "2025" -> "2025"."""
    p = nombre.split(".")
    if len(p) == 1:
        return p[0]
    if len(p) == 3:
        return f"20{p[2][-2:]}-{MESES[p[1][:3].lower()]:02d}-{int(p[0]):02d}"
    if p[0].upper().startswith("T"):
        return f"20{p[1][-2:]}-T{p[0][1:]}"
    return f"{p[1]}-{MESES[p[0][:3].lower()]:02d}"


def _descargar(url, intentos=4):
    """Pide la URL a la API; si responde HTML (límite de pedidos), espera y reintenta. Guarda en caché."""
    if url not in _descargas:
        for i in range(intentos):
            try:
                r = httpx.get(url, timeout=60, headers={"User-Agent": "mcp-bcrp/0.1"})
                _descargas[url] = r.json()
                break
            except (ValueError, httpx.HTTPError):
                if i == intentos - 1:
                    raise ToolError(f"El BCRP no respondió con datos para {url}. Reintenten en unos segundos.")
                time.sleep(2 * (i + 1))
    return _descargas[url]


def _valor(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None   # "n.d." y similares


@mcp.tool(annotations=SOLO_LECTURA)
def serie(codigo: str, desde: str, hasta: str) -> dict:
    """Descarga los datos de una serie del BCRP entre dos fechas. Úsenla cuando necesiten los valores periodo a
    periodo; para variaciones, promedios o extremos usen resumen.

    codigo: el código de buscar_series, por ejemplo "PN01273PM". desde/hasta según la frecuencia de la serie:
    mensual "2025-01", trimestral "2025-T1", diaria "2026-10-15", anual "2025".
    Devuelve el nombre, la frecuencia, los datos [{"periodo", "valor"}] y la URL de la fuente.
    """
    codigo = codigo.strip().upper()
    ficha = _ficha(codigo)
    url = (f"{API}/{codigo}/json/{_fecha_api(desde, ficha['frecuencia'])}/"
           f"{_fecha_api(hasta, ficha['frecuencia'], final=True)}")
    respuesta = _descargar(url)
    datos = [{"periodo": _periodo(p["name"]), "valor": v} for p in respuesta.get("periods", [])
             if (v := _valor(p["values"][0])) is not None]
    if not datos:
        raise ToolError(f"La serie {codigo} no tiene datos entre {desde} y {hasta}. Su primer dato es {ficha['inicio']}.")
    return {"codigo": codigo, "nombre": ficha["nombre"], "frecuencia": ficha["frecuencia"], "datos": datos,
            "fuente": url}


@mcp.tool(annotations=SOLO_LECTURA)
def resumen(codigo: str, desde: str, hasta: str) -> dict:
    """Resume una serie del BCRP entre dos fechas: primer y último valor, cambio absoluto, variación porcentual,
    promedio, máximo y mínimo (con su periodo). Úsenla siempre en vez de hacer cuentas a mano.

    Mismos argumentos que serie, por ejemplo codigo="PN01273PM", desde="2025-12", hasta="2026-09".
    """
    s = serie(codigo, desde, hasta)
    d = pd.DataFrame(s["datos"])
    primero, ultimo = d.iloc[0], d.iloc[-1]
    punto = lambda f: {"periodo": f.periodo, "valor": float(f.valor)}
    return {"codigo": s["codigo"], "nombre": s["nombre"], "frecuencia": s["frecuencia"], "n": len(d),
            "primero": punto(primero), "ultimo": punto(ultimo),
            "cambio": round(float(ultimo.valor - primero.valor), 6),
            "variacion_pct": (round(float((ultimo.valor / primero.valor - 1) * 100), 4) if primero.valor else None),
            "promedio": round(float(d.valor.mean()), 6),
            "maximo": punto(d.loc[d.valor.idxmax()]), "minimo": punto(d.loc[d.valor.idxmin()]),
            "fuente": s["fuente"]}


@mcp.tool(annotations=SOLO_LECTURA)
def comparar_paises(indicador: str = "NY.GDP.MKTP.KD.ZG", paises: str = "PER;CHL;COL;MEX;BRA",
                    desde: int = 2015, hasta: int = 2025) -> dict:
    """Compara al Perú con otros países usando la API del Banco Mundial (el BCRP solo publica datos del Perú).
    Úsenla cuando pregunten cómo le va al Perú frente a la región o a otros países.

    indicador: código del Banco Mundial. Ejemplos: "NY.GDP.MKTP.KD.ZG" (crecimiento del PBI real, %),
    "FP.CPI.TOTL.ZG" (inflación, %), "SL.UEM.TOTL.ZS" (desempleo, %), "NY.GDP.PCAP.CD" (PBI per cápita, US$).
    paises: códigos ISO3 separados por ";", por ejemplo "PER;CHL;COL". desde/hasta: años.
    Devuelve los valores por país y año, y la URL de la fuente.
    """
    url = (f"https://api.worldbank.org/v2/country/{paises}/indicator/{indicador}"
           f"?format=json&date={desde}:{hasta}&per_page=1000")
    try:
        r = httpx.get(url, timeout=60, follow_redirects=True)
        meta, filas = r.json()
    except (ValueError, httpx.HTTPError):
        raise ToolError(f"El Banco Mundial no respondió con datos para {url}.")
    if not filas:
        raise ToolError(f"Sin datos para el indicador {indicador!r} y los países {paises!r}.")
    valores = {}
    for f in filas:
        if f["value"] is not None:
            valores.setdefault(f["country"]["value"], {})[f["date"]] = round(f["value"], 4)
    return {"indicador": indicador, "nombre": filas[0]["indicator"]["value"],
            "valores": {p: dict(sorted(v.items())) for p, v in valores.items()},
            "fuente": url}


@mcp.resource("bcrp://serie/{codigo}", mime_type="application/json")
def ficha_serie(codigo: str) -> dict:
    """Ficha de una serie del BCRP: nombre, categoría, frecuencia, desde cuándo hay datos y última actualización."""
    return _ficha(codigo.strip().upper())


@mcp.prompt
def informe(tema: str) -> str:
    """Pide un informe breve sobre un tema económico del Perú con cifras oficiales del BCRP."""
    return (f"Escribe un informe breve sobre «{tema}» en el Perú con datos oficiales del BCRP.\n"
            "1. Usa buscar_series para encontrar las series relevantes.\n"
            "2. Usa resumen (no cuentas a mano) para la evolución del último año y serie si necesitas el detalle.\n"
            "3. Cita el código de serie, el periodo y la URL de la fuente de cada cifra.\n"
            "4. Cierra con tres puntos clave. Si un dato no existe, dilo en vez de estimarlo.")


if __name__ == "__main__":
    mcp.run()
